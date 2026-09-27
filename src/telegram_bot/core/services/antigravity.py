"""Antigravity CLI (``agy``) engine support.

Everything ``agy``-specific lives here: binary resolution, the process
environment, argv builders, event parsers and conversation discovery. The
session paths in ``claude.py`` (subprocess) and ``tmux_manager.py`` (tmux)
only dispatch into this module.

Protocol facts are recorded in ``docs/antigravity-protocol/README.md``. The
CLI keeps its own OAuth credential under ``AGY_HOME``; this module never reads
it — ``agy`` is exec'd and authenticates itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from telegram_bot.core.messages import t
from telegram_bot.core.services.cc_events import StreamEvent, _tool_status
from telegram_bot.core.services.providers import (
    ExecParseResult,
    _is_safe_owned_executable,
    agent_process_env,
)
from telegram_bot.core.types import ChannelKey

AGY_HOME = Path.home() / ".gemini" / "antigravity-cli"

_CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _agy_home(home: Path | None) -> Path:
    return home if home is not None else AGY_HOME


def _standalone_path() -> Path:
    return Path.home() / ".local" / "bin" / "agy"


def antigravity_binary() -> str:
    """Return a safe absolute ``agy`` path for service processes.

    ``TELEGRAM_AGY_BIN`` overrides discovery and must point at an executable
    owned by the service user; anything else raises instead of silently
    running an untrusted binary.
    """
    configured = os.getenv("TELEGRAM_AGY_BIN")
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute() and _is_safe_owned_executable(path):
            return str(path)
        raise RuntimeError(f"Unsafe configured Antigravity binary: {path}")

    candidates = [_standalone_path()]
    if found := shutil.which("agy"):
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_absolute() and _is_safe_owned_executable(candidate):
            return str(candidate)
    return str(_standalone_path())


def safe_antigravity_binary() -> str | None:
    """Resolve ``agy`` or return ``None`` when no safe executable exists."""
    try:
        candidate = Path(antigravity_binary())
    except RuntimeError:
        return None
    if candidate.is_absolute() and _is_safe_owned_executable(candidate):
        return str(candidate)
    return None


def antigravity_process_env(
    channel_key: ChannelKey | None,
    *,
    binary: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for an ``agy`` process.

    The bot MCP server is registered once in ``agy``'s global config and is
    started as a child of every ``agy`` process, inheriting this environment —
    so the routing variables here are what binds it to the right topic.
    """
    env = agent_process_env(binary=binary, base_env=base_env)
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
    if channel_key is not None:
        chat_id, thread_id = channel_key
        env["TELEGRAM_CHAT_ID"] = str(chat_id)
        env["TELEGRAM_THREAD_ID"] = "" if thread_id is None else str(thread_id)
        env["TELEGRAM_CONTEXT_LOCK"] = "1"
    return env


def is_conversation_id(value: str) -> bool:
    return bool(_CONVERSATION_ID_RE.fullmatch(value))


def brain_dir(conversation_id: str, *, home: Path | None = None) -> Path:
    return _agy_home(home) / "brain" / conversation_id


def transcript_path(conversation_id: str, *, home: Path | None = None) -> Path:
    return (
        brain_dir(conversation_id, home=home)
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )


def _session_flags(conversation_id: str | None, model: str | None) -> list[str]:
    flags: list[str] = []
    if conversation_id:
        flags += ["--conversation", conversation_id]
    if model:
        flags += ["--model", model]
    return flags


def build_exec_argv(
    prompt: str,
    *,
    conversation_id: str | None,
    model: str | None,
    binary: str | None = None,
) -> list[str]:
    """One-shot print-mode run streaming NDJSON events on stdout."""
    return [
        binary or antigravity_binary(),
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
        *_session_flags(conversation_id, model),
    ]


def build_tui_argv(
    *,
    conversation_id: str | None,
    model: str | None,
    binary: str | None = None,
) -> list[str]:
    return [
        binary or antigravity_binary(),
        "--dangerously-skip-permissions",
        *_session_flags(conversation_id, model),
    ]


def build_tui_command(
    channel_key: ChannelKey,
    *,
    conversation_id: str | None,
    model: str | None,
) -> list[str]:
    """Full ``env -i …`` command for a tmux pane, routing variables included."""
    binary = antigravity_binary()
    env = antigravity_process_env(channel_key, binary=binary)
    return [
        "env",
        "-i",
        *(f"{key}={value}" for key, value in sorted(env.items())),
        *build_tui_argv(conversation_id=conversation_id, model=model, binary=binary),
    ]


# --- Tool status lines -------------------------------------------------------

# agy tool → (Claude tool whose label to reuse, agy parameter, Claude parameter).
_CLAUDE_EQUIVALENTS: dict[str, tuple[str, str, str]] = {
    "run_command": ("Bash", "CommandLine", "command"),
    "view_file": ("Read", "AbsolutePath", "file_path"),
    "write_to_file": ("Write", "TargetFile", "file_path"),
    "replace_file_content": ("Edit", "TargetFile", "file_path"),
    "multi_replace_file_content": ("Edit", "TargetFile", "file_path"),
}


def tool_status_line(tool_name: str, parameters: dict[str, Any] | None) -> str:
    """Human-readable status for an agy tool call, in the Claude label style."""
    params = parameters if isinstance(parameters, dict) else {}
    if tool_name == "generate_image":
        return t("tool.generate_image")
    if tool_name == "search_web":
        query = params.get("Query")
        base = t("tool.search_web")
        return f"{base}: {query}" if isinstance(query, str) and query else base
    if tool_name == "call_mcp_tool":
        server = params.get("ServerName")
        method = params.get("ToolName")
        if isinstance(server, str) and isinstance(method, str) and server and method:
            return _tool_status(f"mcp__{server}__{method}")
        return _tool_status("mcp__mcp__call")
    equivalent = _CLAUDE_EQUIVALENTS.get(tool_name)
    if equivalent is not None:
        claude_name, agy_key, claude_key = equivalent
        value = params.get(agy_key)
        return _tool_status(claude_name, {claude_key: value} if isinstance(value, str) else None)
    return _tool_status(tool_name)


# --- Errors ------------------------------------------------------------------

_LOCATION_MARKERS = ("location is not supported", "not currently available in your location")
_AUTH_MARKERS = ("not logged into antigravity", "authentication required")


def friendly_error(error: str) -> str:
    """Map an agy error string to a message that names the likely cause."""
    lowered = error.lower()
    if any(marker in lowered for marker in _LOCATION_MARKERS):
        return t("ui.antigravity_location_error")
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return t("ui.antigravity_auth_error")
    return t("ui.antigravity_error", error=error.strip() or "unknown error")


# --- stream-json (print mode) ------------------------------------------------


class AntigravityExecParser:
    """Stateful parser for one ``agy -p --output-format stream-json`` run.

    Assistant text streams as ``text_delta`` fragments per ``step_index``. A
    completed response step is held back until the next step shows whether it
    was interim commentary (flushed as ``text``) or the final answer, which
    ``result.response`` already carries — so the final answer is sent once.
    """

    def __init__(self) -> None:
        self._text: dict[int, list[str]] = {}
        self._held: str | None = None
        self._images: list[int] = []
        self._agy_error: str | None = None
        self._result_seen = False

    def _flush_held(self) -> list[StreamEvent]:
        held, self._held = self._held, None
        return [StreamEvent("text", held)] if held and held.strip() else []

    def parse(self, line: str) -> ExecParseResult:
        if line.startswith("AGY_ERROR:"):
            payload = _load_json_object(line.removeprefix("AGY_ERROR:").strip())
            short = payload.get("short_error") if payload else None
            self._agy_error = short if isinstance(short, str) else line
            return ExecParseResult([])
        data = _load_json_object(line)
        if data is None:
            return ExecParseResult([])
        kind = data.get("event")
        if kind == "init":
            cid = data.get("conversation_id")
            return ExecParseResult([], session_id=cid if isinstance(cid, str) else None)
        if kind == "step_update":
            step = data.get("step_update")
            return ExecParseResult(self._step(step) if isinstance(step, dict) else [])
        if kind == "result":
            result = data.get("result")
            return ExecParseResult(self._result(result) if isinstance(result, dict) else [])
        return ExecParseResult([])

    def _step(self, step: dict[str, Any]) -> list[StreamEvent]:
        index = step.get("step_index")
        if not isinstance(index, int):
            return []
        step_type = step.get("step_type")
        state = step.get("state")
        if step_type == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                self._text.setdefault(index, []).append(delta)
            if state != "DONE":
                return []
            events = self._flush_held()
            self._held = "".join(self._text.pop(index, [])) or None
            return events
        if step_type == "tool":
            name = step.get("tool_name")
            if not isinstance(name, str):
                return []
            if state == "ACTIVE":
                info = step.get("tool_info")
                params = info.get("parameters") if isinstance(info, dict) else None
                return [*self._flush_held(), StreamEvent("status", tool_status_line(name, params))]
            if state == "DONE" and name == "generate_image":
                self._images.append(index)
        return []

    def _result(self, result: dict[str, Any]) -> list[StreamEvent]:
        self._result_seen = True
        self._held = None
        if result.get("status") == "SUCCESS":
            response = result.get("response")
            return [StreamEvent("result", response if isinstance(response, str) else "")]
        error = result.get("error")
        return [StreamEvent("result", friendly_error(error if isinstance(error, str) else ""))]

    def pending_images(self) -> list[int]:
        """Step indexes of finished ``generate_image`` calls since the last read."""
        images, self._images = self._images, []
        return images

    def finish(self) -> list[StreamEvent]:
        """Events for a run that ended without a ``result`` event."""
        if self._result_seen:
            return []
        if self._agy_error is not None:
            return [StreamEvent("result", friendly_error(self._agy_error))]
        return self._flush_held()


def _load_json_object(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None

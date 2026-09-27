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

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from telegram_bot.core.messages import t
from telegram_bot.core.services.cc_events import StreamEvent, _tool_status
from telegram_bot.core.services.providers import (
    ExecParseResult,
    TuiParseResult,
    _is_safe_owned_executable,
    agent_process_env,
)
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger(__name__)

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


def error_from_stderr(stderr: str) -> str | None:
    """Friendly answer for the last ``AGY_ERROR:`` line on stderr, if any."""
    parser = AntigravityExecParser()
    for line in stderr.splitlines():
        if line.startswith("AGY_ERROR:"):
            parser.parse(line)
    events = parser.finish()
    return events[0].content if events else None


# --- Conversation transcript (tmux mode) -------------------------------------


_GENERATED_MARKER = "Generated image is saved at"


def confined_media_path(uri: str, brain_root: Path | None) -> Path | None:
    """Local path of a transcript ``media.uri``, or ``None`` if it escapes ``brain_root``."""
    raw = uri.removeprefix("file://")
    if not raw.startswith("/") or brain_root is None:
        return None
    path = Path(os.path.realpath(raw))
    root = Path(os.path.realpath(brain_root))
    return path if path.is_relative_to(root) and path != root else None


class AntigravityTranscriptParser:
    """Turn-aware parser for ``brain/<id>/.system_generated/logs/transcript_full.jsonl``.

    One JSON step per line, written while the turn runs. A ``USER_INPUT`` opens
    a turn and closes any dangling one (an Esc-cancelled turn leaves no closing
    step). A ``PLANNER_RESPONSE`` without ``tool_calls`` ends the turn.

    When a background task finishes, agy appends a ``SYSTEM_MESSAGE`` and the
    agent may answer without user input. That answer opens a turn implicitly
    on its first ``PLANNER_RESPONSE``; the system message itself opens nothing,
    so a notice the agent ignores cannot leave the topic stuck "processing".
    """

    def __init__(self, *, brain_root: Path | None) -> None:
        self._brain_root = brain_root
        self._turn_id: str | None = None
        # Tool names of the last response's calls, matched in order to the
        # GENERIC result steps that follow. Only generate_image results are
        # delivered: view_file on an image also carries media, and echoing a
        # screenshot the agent merely looked at back into the chat is wrong.
        self._pending_tools: deque[str] = deque()

    @property
    def current_turn_id(self) -> str | None:
        return self._turn_id

    @staticmethod
    def is_turn_boundary(raw: str) -> bool:
        data = _load_json_object(raw)
        return data is not None and data.get("type") == "USER_INPUT"

    def _open(self, step_index: int) -> list[StreamEvent]:
        events = self._close()
        self._turn_id = f"agy-{step_index}"
        events.append(StreamEvent("turn_start", "", turn_id=self._turn_id))
        return events

    def _close(self) -> list[StreamEvent]:
        turn_id, self._turn_id = self._turn_id, None
        return [StreamEvent("turn_end", "", turn_id=turn_id)] if turn_id is not None else []

    def parse(self, raw: str) -> TuiParseResult:
        data = _load_json_object(raw)
        if data is None:
            return TuiParseResult([])
        index = data.get("step_index")
        if not isinstance(index, int):
            return TuiParseResult([])
        kind = data.get("type")
        if kind == "USER_INPUT":
            return TuiParseResult(self._open(index))
        if kind == "PLANNER_RESPONSE":
            return TuiParseResult(self._planner_response(index, data))
        if kind == "GENERIC":
            return TuiParseResult(self._tool_result(data))
        return TuiParseResult([])

    def _planner_response(self, index: int, data: dict[str, Any]) -> list[StreamEvent]:
        events = self._open(index) if self._turn_id is None else []
        turn_id = self._turn_id
        content = data.get("content")
        text = content if isinstance(content, str) else ""
        tool_calls = data.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            if text.strip():
                events.append(StreamEvent("text", text, turn_id=turn_id))
            self._pending_tools.clear()
            for call in tool_calls:
                name = call.get("name") if isinstance(call, dict) else None
                self._pending_tools.append(name if isinstance(name, str) else "")
                if isinstance(call, dict) and isinstance(name, str):
                    args = call.get("args")
                    status = tool_status_line(name, args if isinstance(args, dict) else None)
                    events.append(StreamEvent("status", status, turn_id=turn_id))
            return events
        if text.strip():
            events.append(StreamEvent("result_message", text, turn_id=turn_id))
        events.extend(self._close())
        return events

    def _tool_result(self, data: dict[str, Any]) -> list[StreamEvent]:
        tool = self._pending_tools.popleft() if self._pending_tools else None
        media = data.get("media")
        if not isinstance(media, list):
            return []
        content = data.get("content")
        generated = tool == "generate_image" or (
            tool is None and isinstance(content, str) and _GENERATED_MARKER in content
        )
        if not generated:
            return []
        events: list[StreamEvent] = []
        for item in media:
            if not isinstance(item, dict):
                continue
            mime = item.get("mime_type")
            uri = item.get("uri")
            if not (isinstance(mime, str) and mime.startswith("image/") and isinstance(uri, str)):
                continue
            path = confined_media_path(uri, self._brain_root)
            if path is None:
                logger.warning("Ignoring agy media outside the conversation: %s", uri)
                continue
            events.append(StreamEvent("image_message", str(path), turn_id=self._turn_id))
        return events


# --- TUI pane state ----------------------------------------------------------

_IDLE_FOOTER = "? for shortcuts"
_BUSY_FOOTER = "esc to cancel"
_TRUST_QUESTION = "Do you trust the contents of this project?"
_TRUST_ACCEPT = "Yes, I trust this folder"
_SELECTION_FOOTER = "↑/↓ Navigate"


def _live_tail(pane: str, lines: int) -> str:
    """The last *lines* non-blank lines — the part of the pane agy redraws."""
    visible = [line for line in pane.splitlines() if line.strip()]
    return "\n".join(visible[-lines:])


def is_trust_dialog(pane: str) -> bool:
    tail = _live_tail(pane, 12)
    return _TRUST_QUESTION in tail and _TRUST_ACCEPT in tail


def is_modal_present(pane: str) -> bool:
    """A selection list (trust dialog, pickers) is blocking the input line."""
    return _SELECTION_FOOTER in _live_tail(pane, 4)


def is_prompt_ready(pane: str) -> bool:
    tail = _live_tail(pane, 6)
    return _IDLE_FOOTER in tail and _BUSY_FOOTER not in tail and not is_modal_present(pane)


# --- Conversation discovery --------------------------------------------------


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _prompt_probe(prompt: str) -> str:
    return _normalize(prompt)[:200]


def snapshot_conversations(*, home: Path | None = None) -> frozenset[str]:
    """Names of the conversation directories that exist right now."""
    root = _agy_home(home) / "brain"
    try:
        return frozenset(entry.name for entry in root.iterdir() if entry.is_dir())
    except OSError:
        return frozenset()


def _user_inputs(path: Path, offset: int = 0) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            lines = handle.read().splitlines()
    except OSError:
        return []
    inputs: list[str] = []
    for line in lines:
        data = _load_json_object(line)
        if data is None or data.get("type") != "USER_INPUT":
            continue
        content = data.get("content")
        if isinstance(content, str):
            inputs.append(_normalize(content))
    return inputs


def transcript_has_user_input(path: Path, offset: int, prompt: str) -> bool:
    """Delivery ack: a ``USER_INPUT`` carrying *prompt* appeared after *offset*."""
    probe = _prompt_probe(prompt)
    return any(probe in text for text in _user_inputs(path, offset))


async def locate_conversation(
    snapshot: frozenset[str],
    prompt: str,
    *,
    home: Path | None = None,
    timeout_sec: float = 30.0,
    poll_sec: float = 0.25,
) -> str | None:
    """Find the conversation a freshly started TUI created for *prompt*.

    Several agy conversations may start at once (other topics, a manual run),
    so a new directory only counts when its first user input carries this
    prompt.
    """
    probe = _prompt_probe(prompt)
    deadline = time.monotonic() + timeout_sec
    while True:
        for cid in sorted(snapshot_conversations(home=home) - snapshot):
            if not is_conversation_id(cid):
                continue
            inputs = _user_inputs(transcript_path(cid, home=home))
            if inputs and probe in inputs[0]:
                return cid
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_sec)


def input_bar_content(pane: str) -> str | None:
    """Text on the ``>`` input line between the two separators, if visible."""
    lines = [line.rstrip() for line in pane.splitlines() if line.strip()]
    for index in range(len(lines) - 2, 0, -1):
        line = lines[index]
        if not line.startswith(">"):
            continue
        if lines[index - 1].startswith("─") and lines[index + 1].startswith("─"):
            return line[1:].strip()
    return None


# --- Bot MCP server ----------------------------------------------------------

_MCP_TIMEOUT_SEC = 30


def _has_mcp_server(list_output: str, name: str) -> bool:
    return any(line.split()[:1] == [name] for line in list_output.splitlines())


def ensure_bot_mcp_registered(
    app_root: Path,
    *,
    binary: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Register this repository's ``bot`` MCP server in agy's global config.

    agy has no per-run MCP flag; the server is registered once and bound to a
    topic by the routing variables of each agy process. Nothing secret goes
    into the config — ``start.sh`` reads the bot token from ``.env``. An
    existing ``bot`` entry is left untouched (the operator may have disabled or
    customised it). Never raises: a failure only costs the engine its bot tools.
    """
    agy = binary or antigravity_binary()
    env = agent_process_env(binary=agy)
    try:
        listed = run(
            [agy, "mcp", "list"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=_MCP_TIMEOUT_SEC,
        )
        if listed.returncode == 0 and _has_mcp_server(listed.stdout, "bot"):
            return True
        added = run(
            [
                agy,
                "mcp",
                "add",
                "-e",
                f"APP_ROOT={app_root}",
                "-e",
                f"ENV_FILE={app_root / '.env'}",
                "-e",
                f"PROJECT_DIR={app_root}",
                "bot",
                "bash",
                str(app_root / "mcp-servers" / "bot" / "start.sh"),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=_MCP_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("Could not register the bot MCP server with agy", exc_info=True)
        return False
    if added.returncode != 0:
        logger.warning("agy mcp add bot failed: %s", (added.stderr or "").strip()[:300])
        return False
    logger.info("Registered the bot MCP server with agy")
    return True


# --- Generated images (subprocess mode) --------------------------------------

# Appended to the first-message preamble: the bot sends generate_image output
# itself, so the agent must not also push it through the bot MCP tools.
PROMPT_NOTE = (
    "\n\nImages you create with the generate_image tool are delivered to this Telegram "
    "chat automatically. Do not send them again with the bot MCP send_image tools.\n\n"
)


def find_generated_images(
    conversation_id: str,
    *,
    steps: Iterable[int],
    home: Path | None = None,
) -> list[Path]:
    """Images produced at *steps* (``generate_image`` step indexes) of a conversation.

    stream-json reports that ``generate_image`` finished but not where the file
    went; the conversation transcript's result step at the same index carries
    it. Paths outside the conversation directory are dropped.
    """
    if not is_conversation_id(conversation_id):
        return []
    wanted = set(steps)
    root = brain_dir(conversation_id, home=home)
    try:
        lines = transcript_path(conversation_id, home=home).read_text(errors="replace").splitlines()
    except OSError:
        return []
    images: list[Path] = []
    for line in lines:
        data = _load_json_object(line)
        if data is None or data.get("step_index") not in wanted or data.get("type") != "GENERIC":
            continue
        media = data.get("media")
        for item in media if isinstance(media, list) else []:
            if not isinstance(item, dict):
                continue
            mime, uri = item.get("mime_type"), item.get("uri")
            if isinstance(mime, str) and mime.startswith("image/") and isinstance(uri, str):
                path = confined_media_path(uri, root)
                if path is not None:
                    images.append(path)
    return images

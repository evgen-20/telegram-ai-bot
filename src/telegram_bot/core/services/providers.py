"""Provider adapters for Claude Code and OpenAI Codex CLI.

The bot has two independent axes:

* provider/engine: which agent CLI to run (Claude or Codex)
* exec_mode: how to run it (subprocess or tmux)

Keeping the provider-specific command and parser contracts here prevents
`exec_mode` from being overloaded with engine names.

Phase 9: the in-tmux Codex TUI integration is gone. Codex is now driven by
`CodexSessionManager` via the `codex app-server` protocol. What stays in
this module: the engine-display helper, the `ExecCommand`/`ExecParseResult`
dataclasses, and `CodexAdapter` reduced to its non-TUI surface — `binary()`
(used by the codex app-server stack) and `parse_exec_event` (used by the
subprocess `codex exec` path in `claude.py`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from telegram_bot.core.services.cc_events import StreamEvent, _tool_status

logger = logging.getLogger(__name__)

Engine = Literal["claude", "codex"]


def engine_display_name(engine: str) -> str:
    """Return a human-facing provider name for chat notifications."""
    if engine == "codex":
        return "Codex"
    if engine == "claude":
        return "Claude Code"
    return engine


@dataclass(frozen=True)
class ExecCommand:
    argv: list[str]
    cwd: str
    stdin_text: str | None = None
    output_last_message_path: Path | None = None


@dataclass(frozen=True)
class ExecParseResult:
    events: list[StreamEvent]
    session_id: str | None = None


class ProviderAdapter(Protocol):
    name: Engine

    def parse_exec_event(self, raw: str) -> ExecParseResult: ...


def _load_json(raw: str) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class CodexAdapter:
    name: Engine = "codex"

    @staticmethod
    def _command_from_exec_payload(payload: dict[str, Any]) -> str | None:
        command = payload.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            parts = [str(part) for part in command]
            if len(parts) >= 3 and parts[0].endswith("bash") and parts[1] == "-lc":
                return parts[2]
            return " ".join(parts)
        if isinstance(command, str) and command:
            return command

        parsed_cmd = payload.get("parsed_cmd")
        if isinstance(parsed_cmd, list):
            for item in parsed_cmd:
                if isinstance(item, dict):
                    cmd = item.get("cmd")
                    if isinstance(cmd, str) and cmd:
                        return cmd
        return None

    @staticmethod
    def _message_text_from_payload(payload: dict[str, Any]) -> str | None:
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
        text = payload.get("text")
        if isinstance(text, str) and text:
            return text
        content = payload.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_text = item.get("text")
                if isinstance(item_text, str) and item_text:
                    parts.append(item_text)
            if parts:
                return "".join(parts)
        return None

    @staticmethod
    def _status_for_codex_function_call(name: str, tool_input: dict[str, object] | None) -> str:
        if name.startswith("functions."):
            name = name.split(".", 1)[1]
        if name == "exec_command" and isinstance(tool_input, dict):
            cmd = tool_input.get("cmd")
            if isinstance(cmd, str) and cmd:
                return _tool_status("Bash", {"command": cmd})
        return _tool_status(name, tool_input)

    def binary(self) -> str:
        """Return an executable Codex CLI path that works in service processes.

        The bot service may not inherit the interactive shell PATH, while
        npm-global commonly installs Codex under ~/.npm-global/bin. Using the
        absolute path prevents tmux from opening and immediately closing with
        "codex: command not found", which otherwise looks like a TUI readiness
        timeout.
        """
        fallback = Path.home() / ".npm-global" / "bin" / "codex"
        if self._is_safe_binary(fallback):
            return str(fallback)
        if found := shutil.which("codex"):
            candidate = Path(found)
            if candidate.is_absolute() and self._is_safe_binary(candidate):
                return str(candidate)
        # Return the explicit expected path so process spawn fails loudly
        # instead of searching a service PATH that may not contain Codex.
        return str(fallback)

    @staticmethod
    def _is_safe_binary(path: Path) -> bool:
        try:
            stat = path.stat()
        except OSError:
            return False
        if not path.is_file() or not os.access(path, os.X_OK):
            return False
        return stat.st_uid == os.getuid() and stat.st_mode & 0o022 == 0

    def parse_exec_event(self, raw: str) -> ExecParseResult:
        data = _load_json(raw)
        if data is None:
            return ExecParseResult([])

        event_type = data.get("type")
        if event_type == "thread.started":
            thread_id = data.get("thread_id")
            return ExecParseResult([], thread_id if isinstance(thread_id, str) else None)

        payload = data.get("payload")
        if event_type == "response_item" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "function_call":
                name = payload.get("name", "")
                args = payload.get("arguments")
                tool_input: dict[str, object] | None = None
                if isinstance(args, str):
                    parsed_args = _load_json(args)
                    tool_input = parsed_args if parsed_args is not None else None
                elif isinstance(args, dict):
                    tool_input = args
                status = self._status_for_codex_function_call(str(name), tool_input)
                return ExecParseResult([StreamEvent("status", status)])
            if payload_type == "tool_search_call":
                return ExecParseResult([StreamEvent("status", "Ищу инструмент...")])
            if payload_type == "message" and payload.get("role") == "assistant":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])

        if event_type == "event_msg" and isinstance(payload, dict):
            payload_type = payload.get("type")
            if payload_type == "agent_message":
                if payload.get("phase") == "final_answer":
                    return ExecParseResult([])
                text = self._message_text_from_payload(payload)
                if text:
                    return ExecParseResult([StreamEvent("text", text)])
                return ExecParseResult([])
            if payload_type == "exec_command_end":
                exit_code = payload.get("exit_code")
                if not isinstance(exit_code, int) or exit_code == 0:
                    return ExecParseResult([])
                command = self._command_from_exec_payload(payload)
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                return ExecParseResult([StreamEvent("status", f"{status} (exit {exit_code})")])

        item = data.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "command_execution":
                command = item.get("command")
                exit_code = item.get("exit_code")
                if event_type == "item.completed" and (
                    not isinstance(exit_code, int) or exit_code == 0
                ):
                    return ExecParseResult([])
                status = _tool_status(
                    "Bash",
                    {"command": command} if isinstance(command, str) else None,
                )
                if event_type == "item.completed" and isinstance(exit_code, int) and exit_code:
                    status = f"{status} (exit {exit_code})"
                return ExecParseResult([StreamEvent("status", status)])
            if event_type == "item.completed" and item_type == "agent_message":
                # Final answer is read from --output-last-message after exit.
                return ExecParseResult([])

        return ExecParseResult([])


CODEX_ADAPTER = CodexAdapter()


def is_engine_available(engine: str) -> bool:
    """Return whether the provider CLI can be spawned by the current process."""
    if engine == "claude":
        return shutil.which("claude") is not None
    if engine == "codex":
        return CODEX_ADAPTER._is_safe_binary(Path(CODEX_ADAPTER.binary()))
    return False


def choose_available_engine(preferred: str = "claude") -> Engine | None:
    """Pick an installed engine, preferring the requested one and then the other."""
    if preferred in {"claude", "codex"} and is_engine_available(preferred):
        return preferred  # type: ignore[return-value]
    fallback: Engine = "codex" if preferred == "claude" else "claude"
    if is_engine_available(fallback):
        return fallback
    return None

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

import os
import re
import shutil
from pathlib import Path

from telegram_bot.core.services.providers import _is_safe_owned_executable, agent_process_env
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

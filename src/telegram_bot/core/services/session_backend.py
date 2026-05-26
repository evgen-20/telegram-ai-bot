"""Backend abstraction for Telegram bot session managers.

`SessionBackend` is the surface area used by Telegram handlers. Both the
tmux-based path (claude) and the codex app-server path implement it.
`BackendDispatcher` picks the right one for a topic's engine.

Methods unique to one backend (tmux modal watchdog; codex thread/resume)
stay private to that backend's concrete class.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.cc_modes import Mode
from telegram_bot.core.services.tmux_manager import SwitchResult
from telegram_bot.core.services.topic_config import Engine, TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults
from telegram_bot.core.types import ChannelKey


@runtime_checkable
class SessionBackend(Protocol):
    """The slice of session-management used by Telegram handlers.

    Each method matches an existing tmux_manager.TmuxManager method by name
    and signature so callers can be migrated one site at a time.
    """

    def is_active(self, channel_key: ChannelKey) -> bool: ...
    def is_processing(self, channel_key: ChannelKey) -> bool: ...
    def is_tailing(self, channel_key: ChannelKey) -> bool: ...

    def get_session_id(self, channel_key: ChannelKey) -> str | None: ...
    def get_session_name(self, channel_key: ChannelKey) -> str | None: ...
    def get_provider_model(self, channel_key: ChannelKey) -> tuple[str | None, str | None]: ...

    async def start_session(
        self,
        channel_key: ChannelKey,
        *,
        mode: Mode,
        cwd: str,
        mcp_config: str,
        chat_id: int,
        session_manager: object,
        resume_session_id: str | None = None,
        provider: str = "claude",
        model: str | None = None,
    ) -> None: ...

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str: ...

    async def send_direct(self, channel_key: ChannelKey, prompt: str) -> bool: ...

    async def close_buffer(self, channel_key: ChannelKey) -> None: ...
    async def cancel(self, channel_key: ChannelKey) -> None: ...
    async def kill(self, channel_key: ChannelKey) -> None: ...

    async def clear_context(self, channel_key: ChannelKey, session_manager: object) -> bool: ...

    async def switch_session(
        self,
        channel_key: ChannelKey,
        new_session_id: str,
        session_manager: object,
    ) -> bool: ...

    async def switch_or_start_session(
        self,
        channel_key: ChannelKey,
        target_session_id: str,
        target_provider: Engine,
        target_transcript_path: Path,
        *,
        session_manager: object,
        topic_config: TopicConfig,
        defaults: BotDefaults,
    ) -> SwitchResult: ...


class BackendDispatcher:
    """Returns the right backend for a given engine name."""

    def __init__(self, *, claude: SessionBackend, codex: SessionBackend) -> None:
        self._by_engine: dict[Engine, SessionBackend] = {
            "claude": claude,
            "codex": codex,
        }

    def for_engine(self, engine: Engine) -> SessionBackend:
        try:
            return self._by_engine[engine]
        except KeyError as exc:
            raise ValueError(f"unknown engine: {engine!r}") from exc

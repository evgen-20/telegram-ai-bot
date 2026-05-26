"""Codex session orchestrator.

Owns one ``CodexAppServerClient`` per Telegram channel. Drives:

- ``initialize`` + ``thread/start`` on first message in a topic
- ``thread/resume`` on bot restart
- ``turn/start`` per user message; streams events through ``on_event``
- ``turn/interrupt`` on cancel
- subprocess teardown on close/kill

Implements the ``SessionBackend`` Protocol so the bot's handlers can treat
codex and the tmux-claude backend uniformly. Parameters that only make
sense for the tmux/claude backend (``mode``, ``session_manager``,
``mcp_config`` for tmux start-up, etc.) are accepted to satisfy the
Protocol and intentionally ignored here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.cc_modes import Mode
from telegram_bot.core.services.codex_app_server import CodexAppServerClient
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_events import parse_codex_notification
from telegram_bot.core.services.tmux_manager import SwitchResult
from telegram_bot.core.services.tmux_state import load_codex_sessions
from telegram_bot.core.services.topic_config import Engine, TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger("codex_session_manager")

DEFAULT_PROXY_COMMAND: tuple[str, ...] = ("codex", "app-server", "proxy")

# How long a single turn is allowed to run before we give up and call
# ``turn/interrupt``. Mirrors the tmux backend's per-turn budget.
_TURN_TIMEOUT_SEC = 300.0


class _ClientFactory(Protocol):
    """Shape of a callable used to construct a Codex app-server client.

    Used so tests can swap a fake in without subclassing ``CodexAppServerClient``.
    """

    def __call__(
        self,
        *,
        command: list[str],
        on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> CodexAppServerClient: ...


@dataclass
class CodexSessionState:
    """Per-channel runtime state for a Codex session."""

    channel_key: ChannelKey
    thread_id: str
    cwd: str
    model: str | None
    # ``client`` is ``None`` for state restored from disk until the first
    # ``send_stream`` lazily reconnects (see Task 7.3).
    client: CodexAppServerClient | None
    notif_queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    current_turn_id: str | None = None
    is_processing: bool = False
    is_active: bool = True


class CodexSessionManager:
    """SessionBackend implementation backed by ``codex app-server``."""

    def __init__(
        self,
        *,
        daemon: CodexDaemonManager,
        state_path: Path,
        proxy_command_factory: Callable[[], list[str]] | None = None,
    ) -> None:
        self._daemon = daemon
        self._state_path = state_path
        self._proxy_command_factory: Callable[[], list[str]] = (
            proxy_command_factory
            if proxy_command_factory is not None
            else (lambda: list(DEFAULT_PROXY_COMMAND))
        )
        self._sessions: dict[ChannelKey, CodexSessionState] = {}
        # Lock per channel — serialises start/send/cancel for one channel
        # without blocking other channels.
        self._channel_locks: dict[ChannelKey, asyncio.Lock] = {}
        # Tests override; production uses the real client.
        self._client_factory: _ClientFactory = CodexAppServerClient

    # ---- SessionBackend slice (sync inspectors) ------------------------

    def is_active(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return bool(s and s.is_active)

    def is_processing(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return bool(s and s.is_processing)

    def is_tailing(self, channel_key: ChannelKey) -> bool:
        # Codex streams notifications inline rather than tailing a transcript.
        # Telegram callers only use ``is_tailing`` to gate concurrent prompts,
        # so the same flag as ``is_processing`` is semantically correct.
        return self.is_processing(channel_key)

    def get_session_id(self, channel_key: ChannelKey) -> str | None:
        s = self._sessions.get(channel_key)
        return s.thread_id if s else None

    def get_session_name(self, channel_key: ChannelKey) -> str | None:
        # Codex has no tmux session name; reuse thread_id for any caller that
        # logs or displays the "session name".
        return self.get_session_id(channel_key)

    def get_provider_model(self, channel_key: ChannelKey) -> tuple[str | None, str | None]:
        s = self._sessions.get(channel_key)
        if s is None:
            return None, None
        return "codex", s.model

    # ---- SessionBackend slice (async lifecycle) ------------------------

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
    ) -> None:
        """Spawn a Codex proxy client and open (or resume) a thread.

        ``mode``, ``mcp_config``, ``chat_id``, ``session_manager`` and
        ``provider`` are accepted to satisfy the Protocol but are not used
        by the Codex backend — Codex configures itself through the daemon
        and ``thread/start`` parameters instead.
        """
        del mode, mcp_config, chat_id, session_manager, provider

        await self._daemon.ensure_running()

        async with self._get_channel_lock(channel_key):
            # If a previous session exists for this channel, close it before
            # opening the new one — start_session must be idempotent.
            existing = self._sessions.pop(channel_key, None)
            if existing is not None and existing.client is not None:
                with contextlib.suppress(Exception):
                    await existing.client.close()

            client, notif_queue, thread_id = await self._spawn_and_initialize_client(
                cwd=cwd, model=model, resume_thread_id=resume_session_id
            )

            state = CodexSessionState(
                channel_key=channel_key,
                thread_id=thread_id,
                cwd=cwd,
                model=model,
                client=client,
                notif_queue=notif_queue,
            )
            self._sessions[channel_key] = state
            self._persist()

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str:
        """Drive one ``turn/start`` and stream notifications until completion."""
        state = self._sessions.get(channel_key)
        if state is None:
            raise KeyError(f"no codex session for channel {channel_key!r}")
        if state.client is None:
            # State was restored from disk (see restore_all). Reconnect now:
            # ensure the daemon is up, spawn a fresh proxy client, and resume
            # the persisted thread before driving the turn.
            await self._daemon.ensure_running()
            async with self._get_channel_lock(channel_key):
                # Re-check under lock to avoid double-spawn from concurrent sends.
                if state.client is None:
                    client, notif_queue, _ = await self._spawn_and_initialize_client(
                        cwd=state.cwd,
                        model=state.model,
                        resume_thread_id=state.thread_id,
                    )
                    state.client = client
                    state.notif_queue = notif_queue
                    state.is_active = True

        state.is_processing = True
        notif_queue = state.notif_queue
        client = state.client
        assert client is not None  # narrow for mypy; populated just above
        thread_id = state.thread_id
        try:
            await client.turn_start(thread_id=thread_id, prompt=prompt)
            while True:
                try:
                    notif = await asyncio.wait_for(notif_queue.get(), timeout=_TURN_TIMEOUT_SEC)
                except TimeoutError:
                    logger.warning("codex turn timeout for %s; interrupting", channel_key)
                    if state.current_turn_id is not None:
                        with contextlib.suppress(Exception):
                            await client.turn_interrupt(
                                thread_id=thread_id, turn_id=state.current_turn_id
                            )
                    break

                if notif.get("method") == "turn/started":
                    params = notif.get("params") or {}
                    turn_id = params.get("turnId") if isinstance(params, dict) else None
                    if isinstance(turn_id, str):
                        state.current_turn_id = turn_id

                events = parse_codex_notification(notif)
                done = False
                for ev in events:
                    ret = on_event(ev)
                    if asyncio.iscoroutine(ret):
                        await ret
                    if ev.type == "result":
                        done = True
                if done:
                    break
        finally:
            state.is_processing = False
            state.current_turn_id = None
        return ""

    async def send_direct(self, channel_key: ChannelKey, prompt: str) -> bool:
        """Non-streaming send: drain the turn and report success."""

        async def _discard(_ev: StreamEvent) -> None:
            return None

        await self.send_stream(channel_key, prompt, _discard)
        return True

    async def close_buffer(self, channel_key: ChannelKey) -> None:
        """Close the codex client for this channel. Idempotent."""
        state = self._sessions.pop(channel_key, None)
        if state is None:
            return
        state.is_active = False
        if state.client is not None:
            with contextlib.suppress(Exception):
                await state.client.close()
        self._persist()

    async def cancel(self, channel_key: ChannelKey) -> None:
        """Best-effort ``turn/interrupt`` for the active turn."""
        state = self._sessions.get(channel_key)
        if state is None or state.client is None or state.current_turn_id is None:
            return
        with contextlib.suppress(Exception):
            await state.client.turn_interrupt(
                thread_id=state.thread_id, turn_id=state.current_turn_id
            )

    async def kill(self, channel_key: ChannelKey) -> None:
        """Tear down this channel's session; same as ``close_buffer`` here."""
        await self.close_buffer(channel_key)

    async def clear_context(self, channel_key: ChannelKey, session_manager: object) -> bool:
        """Drop the current thread and start a fresh one on the same cwd."""
        del session_manager
        state = self._sessions.get(channel_key)
        if state is None:
            return False
        cwd = state.cwd
        model = state.model
        await self.close_buffer(channel_key)
        await self.start_session(
            channel_key,
            mode="free",
            cwd=cwd,
            mcp_config="",
            chat_id=channel_key[0],
            session_manager=object(),
            resume_session_id=None,
            provider="codex",
            model=model,
        )
        return True

    async def switch_session(
        self,
        channel_key: ChannelKey,
        new_session_id: str,
        session_manager: object,
    ) -> bool:
        """Resume a different thread for this channel. Returns True on success."""
        del session_manager
        state = self._sessions.get(channel_key)
        cwd = state.cwd if state is not None else "/tmp"
        model = state.model if state is not None else None
        try:
            await self.close_buffer(channel_key)
            await self.start_session(
                channel_key,
                mode="free",
                cwd=cwd,
                mcp_config="",
                chat_id=channel_key[0],
                session_manager=object(),
                resume_session_id=new_session_id,
                provider="codex",
                model=model,
            )
        except Exception:
            logger.warning(
                "codex switch_session failed channel=%s session=%s",
                channel_key,
                new_session_id,
                exc_info=True,
            )
            return False
        return True

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
    ) -> SwitchResult:
        """Codex equivalent of the tmux backend's switch-or-start.

        The codex backend has no transcript file and no tmux pane, so it
        boils down to ``thread/resume`` on the target id. The tmux-shaped
        bookkeeping (engine/mode-change book-keeping, transcript existence
        checks) is delegated to the caller's choice of backend.
        """
        del target_transcript_path, session_manager, topic_config, defaults

        if target_provider != "codex":
            # The dispatcher should have routed elsewhere; refuse loudly.
            return SwitchResult(kind="invalid_id")

        captured = self._sessions.get(channel_key)
        if captured is not None and captured.is_active and captured.thread_id == target_session_id:
            return SwitchResult(kind="already_on_it")

        had_existing = captured is not None
        try:
            cwd = captured.cwd if captured is not None else "/tmp"
            model = captured.model if captured is not None else None
            await self.close_buffer(channel_key)
            await self.start_session(
                channel_key,
                mode="free",
                cwd=cwd,
                mcp_config="",
                chat_id=channel_key[0],
                session_manager=object(),
                resume_session_id=target_session_id,
                provider="codex",
                model=model,
            )
        except Exception:
            logger.warning(
                "codex switch_or_start_session failed channel=%s session=%s",
                channel_key,
                target_session_id,
                exc_info=True,
            )
            return SwitchResult(kind="spawn_failed")
        return SwitchResult(kind="switched" if had_existing else "started")

    # ---- persistence ---------------------------------------------------

    def _persist(self) -> None:
        try:
            existing_text = self._state_path.read_text()
            existing = json.loads(existing_text) if existing_text.strip() else {}
        except (FileNotFoundError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["codex_sessions"] = {
            f"{k[0]}:{k[1]}": {"thread_id": s.thread_id, "cwd": s.cwd}
            for k, s in self._sessions.items()
        }
        self._state_path.write_text(json.dumps(existing, indent=2))

    def restore_all(self) -> None:
        """Load persisted codex sessions; client is lazily reconnected later."""
        records = load_codex_sessions(self._state_path)
        for channel, record in records.items():
            state = CodexSessionState(
                channel_key=channel,
                thread_id=record.thread_id,
                cwd=record.cwd,
                model=None,
                client=None,
            )
            state.is_active = False  # not usable until next send_stream reconnects
            self._sessions[channel] = state

    # ---- internals -----------------------------------------------------

    def _get_channel_lock(self, channel_key: ChannelKey) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_key] = lock
        return lock

    async def _spawn_and_initialize_client(
        self,
        *,
        cwd: str,
        model: str | None,
        resume_thread_id: str | None,
    ) -> tuple[CodexAppServerClient, asyncio.Queue[dict[str, Any]], str]:
        """Spawn a proxy client, initialize it, and start/resume a thread.

        Shared by ``start_session`` (fresh spawn) and ``send_stream`` lazy
        reconnect (after ``restore_all`` from disk). Returns the live client,
        its notification queue, and the active ``thread_id``.

        If ``resume_thread_id`` is provided but ``thread/resume`` fails, falls
        back to a fresh ``thread/start`` and returns the new id.
        """
        notif_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_notification(notif: dict[str, Any]) -> None:
            await notif_queue.put(notif)

        client = self._client_factory(
            command=self._proxy_command_factory(),
            on_notification=on_notification,
        )
        await client.__aenter__()
        try:
            await client.initialize(client_name="telegram-ai-agent", client_version="1.0")

            if resume_thread_id is not None:
                try:
                    await client.thread_resume(thread_id=resume_thread_id)
                    thread_id = resume_thread_id
                except Exception:
                    logger.warning(
                        "thread_resume failed for %s; starting fresh",
                        resume_thread_id,
                        exc_info=True,
                    )
                    ts = await client.thread_start(cwd=cwd, model=model)
                    thread_id = ts["threadId"]
            else:
                ts = await client.thread_start(cwd=cwd, model=model)
                thread_id = ts["threadId"]
        except Exception:
            with contextlib.suppress(Exception):
                await client.close()
            raise

        return client, notif_queue, thread_id

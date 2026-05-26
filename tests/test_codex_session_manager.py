"""High-level CodexSessionManager tests — happy path."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_session_manager import CodexSessionManager
from telegram_bot.core.services.session_backend import SessionBackend


@pytest.fixture
def daemon_mock() -> AsyncMock:
    m = AsyncMock(spec=CodexDaemonManager)
    m.ensure_running = AsyncMock()
    return m


def test_codex_session_manager_satisfies_protocol() -> None:
    # Compile-time check (mypy in CI catches signature drift); runtime check
    # is duck-typed via @runtime_checkable Protocol attributes.
    _check_assignable: type[SessionBackend] = CodexSessionManager
    del _check_assignable


class _FakeClient:
    """Stub CodexAppServerClient that scripts a happy turn over the notification hook."""

    thread_id = "th-1"

    def __init__(
        self,
        *,
        command: list[str],
        on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        self._command = command
        self._on_notification = on_notification
        self.closed = False
        self.initialize_called = False
        self.thread_start_called = False
        self.last_prompt: str | None = None

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def initialize(self, *, client_name: str, client_version: str) -> dict[str, Any]:
        self.initialize_called = True
        return {"serverInfo": {"name": "codex", "version": client_version}}

    async def thread_start(self, *, cwd: str, model: str | None) -> dict[str, Any]:
        self.thread_start_called = True
        return {"threadId": self.thread_id}

    async def thread_resume(self, *, thread_id: str) -> None:
        return None

    async def turn_start(self, *, thread_id: str, prompt: str) -> None:
        self.last_prompt = prompt
        # Script: agentMessage delta -> agentMessage completed (final) -> turn/completed.
        await self._emit(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": thread_id, "itemId": "i1", "delta": "Hi!"},
            }
        )
        await self._emit(
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "item": {
                        "id": "i1",
                        "type": "agentMessage",
                        "text": "Hi!",
                        "phase": "final_answer",
                        "status": "completed",
                    },
                },
            }
        )
        await self._emit(
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turnId": "t1", "status": "completed"},
            }
        )

    async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def _emit(self, notif: dict[str, Any]) -> None:
        ret = self._on_notification(notif)
        if hasattr(ret, "__await__"):
            await ret  # type: ignore[misc]


@pytest.mark.asyncio
async def test_start_session_then_send_stream_happy_path(
    daemon_mock: AsyncMock, tmp_path: Path
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )
    # Inject the fake client factory before start_session spawns anything.
    mgr._client_factory = _FakeClient  # type: ignore[assignment]

    captured: list[StreamEvent] = []

    async def on_event(ev: StreamEvent) -> None:
        captured.append(ev)

    channel = (-1001, 99)
    await mgr.start_session(
        channel,
        mode="free",
        cwd="/tmp",
        mcp_config="",
        chat_id=channel[0],
        session_manager=object(),
        resume_session_id=None,
        provider="codex",
        model=None,
    )

    daemon_mock.ensure_running.assert_awaited_once()
    assert mgr.is_active(channel) is True
    assert mgr.get_session_id(channel) == _FakeClient.thread_id
    assert mgr.get_provider_model(channel) == ("codex", None)

    result = await mgr.send_stream(channel, "hello", on_event)

    assert result == ""
    types = [e.type for e in captured]
    assert "text" in types
    assert "result_message" in types
    assert "result" in types
    # Order matters for streaming UX: deltas first, final message, then result sentinel.
    assert types.index("text") < types.index("result_message") < types.index("result")
    # Persistence: codex_sessions entry written.
    persisted = json.loads(state_path.read_text())
    assert "codex_sessions" in persisted
    assert f"{channel[0]}:{channel[1]}" in persisted["codex_sessions"]
    assert persisted["codex_sessions"][f"{channel[0]}:{channel[1]}"]["thread_id"] == "th-1"


@pytest.mark.asyncio
async def test_send_stream_emits_image_message(daemon_mock: AsyncMock, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )

    captured: list[StreamEvent] = []

    async def on_event(ev: StreamEvent) -> None:
        captured.append(ev)

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG" + b"\0" * 6000)

    class FakeClient:
        def __init__(
            self,
            *,
            command: list[str],
            on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
        ) -> None:
            self._on_notification = on_notification

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self, *, client_name: str, client_version: str) -> dict[str, Any]:
            return {"serverInfo": {}}

        async def thread_start(self, *, cwd: str, model: str | None) -> dict[str, Any]:
            return {"threadId": "t"}

        async def turn_start(self, *, thread_id: str, prompt: str) -> None:
            await self._emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "item": {
                            "id": "i",
                            "type": "imageGeneration",
                            "status": "completed",
                            "savedPath": str(img),
                            "revisedPrompt": "cat",
                        },
                    },
                }
            )
            await self._emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "t1",
                        "status": "completed",
                    },
                }
            )

        async def close(self) -> None:
            return None

        async def _emit(self, notif: dict[str, Any]) -> None:
            ret = self._on_notification(notif)
            if hasattr(ret, "__await__"):
                await ret  # type: ignore[misc]

    mgr._client_factory = FakeClient  # type: ignore[assignment]
    ch = (-1, 1)
    await mgr.start_session(
        ch,
        mode="free",
        cwd="/tmp",
        mcp_config="",
        chat_id=ch[0],
        session_manager=object(),
        resume_session_id=None,
        provider="codex",
        model=None,
    )
    await mgr.send_stream(ch, "draw a cat", on_event)

    assert any(e.type == "image_message" and e.content == str(img) for e in captured)


@pytest.mark.asyncio
async def test_cancel_invokes_turn_interrupt(daemon_mock: AsyncMock, tmp_path: Path) -> None:
    """During an active turn, ``mgr.cancel`` must call ``turn_interrupt``."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )

    interrupt_calls: list[tuple[str, str]] = []
    started_event = asyncio.Event()

    class CancelFakeClient:
        thread_id = "th-cancel"

        def __init__(
            self,
            *,
            command: list[str],
            on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
        ) -> None:
            self._on_notification = on_notification

        async def __aenter__(self) -> CancelFakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self, *, client_name: str, client_version: str) -> dict[str, Any]:
            return {"serverInfo": {}}

        async def thread_start(self, *, cwd: str, model: str | None) -> dict[str, Any]:
            return {"threadId": self.thread_id}

        async def turn_start(self, *, thread_id: str, prompt: str) -> None:
            # Emit only turn/started so the manager records current_turn_id;
            # do NOT emit turn/completed — cancel will be triggered externally.
            await self._emit(
                {
                    "method": "turn/started",
                    "params": {"threadId": thread_id, "turnId": "t-cancel"},
                }
            )
            started_event.set()

        async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
            interrupt_calls.append((thread_id, turn_id))
            # Once interrupted, the server would send turn/completed; emulate
            # that here so send_stream can exit cleanly.
            await self._emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "status": "cancelled",
                    },
                }
            )

        async def close(self) -> None:
            return None

        async def _emit(self, notif: dict[str, Any]) -> None:
            ret = self._on_notification(notif)
            if hasattr(ret, "__await__"):
                await ret  # type: ignore[misc]

    mgr._client_factory = CancelFakeClient  # type: ignore[assignment]

    ch = (-100, 7)
    await mgr.start_session(
        ch,
        mode="free",
        cwd="/tmp",
        mcp_config="",
        chat_id=ch[0],
        session_manager=object(),
        resume_session_id=None,
        provider="codex",
        model=None,
    )

    async def _noop(_ev: StreamEvent) -> None:
        return None

    send_task = asyncio.create_task(mgr.send_stream(ch, "long task", _noop))
    # Wait until the fake client has scripted turn/started so the manager
    # has captured the turn_id.
    await asyncio.wait_for(started_event.wait(), timeout=2.0)
    # Give the event loop a turn so the manager processes the queued
    # turn/started notification before we cancel.
    await asyncio.sleep(0)
    await mgr.cancel(ch)
    await asyncio.wait_for(send_task, timeout=2.0)

    assert interrupt_calls == [("th-cancel", "t-cancel")]


@pytest.mark.asyncio
async def test_clear_context_replaces_thread_id(daemon_mock: AsyncMock, tmp_path: Path) -> None:
    """``clear_context`` must drop the existing thread and start a fresh one."""
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )

    # Each new fake client instance returns a fresh, unique thread id so the
    # test can assert the manager swapped to a new thread after clear_context.
    counter = {"n": 0}

    class ClearFakeClient:
        def __init__(
            self,
            *,
            command: list[str],
            on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
        ) -> None:
            counter["n"] += 1
            self._instance_id = counter["n"]
            self._on_notification = on_notification

        async def __aenter__(self) -> ClearFakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self, *, client_name: str, client_version: str) -> dict[str, Any]:
            return {"serverInfo": {}}

        async def thread_start(self, *, cwd: str, model: str | None) -> dict[str, Any]:
            return {"threadId": f"th-{self._instance_id}"}

        async def thread_resume(self, *, thread_id: str) -> None:
            return None

        async def turn_start(self, *, thread_id: str, prompt: str) -> None:
            return None

        async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
            return None

        async def close(self) -> None:
            return None

    mgr._client_factory = ClearFakeClient  # type: ignore[assignment]

    ch = (-200, 3)
    await mgr.start_session(
        ch,
        mode="free",
        cwd="/tmp",
        mcp_config="",
        chat_id=ch[0],
        session_manager=object(),
        resume_session_id=None,
        provider="codex",
        model=None,
    )
    before = mgr.get_session_id(ch)
    assert before == "th-1"

    cleared = await mgr.clear_context(ch, session_manager=object())
    assert cleared is True

    after = mgr.get_session_id(ch)
    assert after is not None
    assert after != before
    assert after == "th-2"


@pytest.mark.asyncio
async def test_restore_all_then_send_stream_lazy_reconnect(
    daemon_mock: AsyncMock, tmp_path: Path
) -> None:
    """``restore_all`` loads persisted state; the next ``send_stream`` reconnects.

    The reconnect must spawn a client (factory called) with the persisted
    thread_id used for ``thread_resume``.
    """
    state_path = tmp_path / "state.json"
    persisted_thread_id = "th-restored-xyz"
    state_path.write_text(
        json.dumps(
            {
                "codex_sessions": {
                    "-300:5": {
                        "thread_id": persisted_thread_id,
                        "cwd": "/tmp",
                    },
                }
            }
        )
    )

    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )

    resume_calls: list[str] = []
    factory_calls: list[dict[str, Any]] = []

    class ResumeFakeClient:
        def __init__(
            self,
            *,
            command: list[str],
            on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
        ) -> None:
            factory_calls.append({"command": command})
            self._on_notification = on_notification

        async def __aenter__(self) -> ResumeFakeClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self, *, client_name: str, client_version: str) -> dict[str, Any]:
            return {"serverInfo": {}}

        async def thread_start(self, *, cwd: str, model: str | None) -> dict[str, Any]:
            return {"threadId": "should-not-be-used"}

        async def thread_resume(self, *, thread_id: str) -> None:
            resume_calls.append(thread_id)

        async def turn_start(self, *, thread_id: str, prompt: str) -> None:
            # Drive a minimal happy turn to completion.
            await self._emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "t-r1",
                        "status": "completed",
                    },
                }
            )

        async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
            return None

        async def close(self) -> None:
            return None

        async def _emit(self, notif: dict[str, Any]) -> None:
            ret = self._on_notification(notif)
            if hasattr(ret, "__await__"):
                await ret  # type: ignore[misc]

    mgr._client_factory = ResumeFakeClient  # type: ignore[assignment]

    mgr.restore_all()

    ch = (-300, 5)
    # After restore_all the session is marked inactive (client not spawned yet).
    assert mgr.get_session_id(ch) == persisted_thread_id
    assert mgr.is_active(ch) is False
    assert factory_calls == []  # no spawn yet

    captured: list[StreamEvent] = []

    async def on_event(ev: StreamEvent) -> None:
        captured.append(ev)

    await mgr.send_stream(ch, "follow-up after restart", on_event)

    # The lazy reconnect must have spawned exactly one client and resumed the
    # persisted thread.
    assert len(factory_calls) == 1
    assert resume_calls == [persisted_thread_id]
    # Session is now active and still reports the persisted thread id.
    assert mgr.is_active(ch) is True
    assert mgr.get_session_id(ch) == persisted_thread_id
    # Daemon was ensured at least once (start_session was never called; this
    # is the reconnect path).
    assert daemon_mock.ensure_running.await_count >= 1
    # The turn completed via the scripted notification.
    assert any(e.type == "result" for e in captured)

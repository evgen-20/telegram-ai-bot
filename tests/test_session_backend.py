"""Tests for the SessionBackend Protocol and BackendDispatcher."""

from __future__ import annotations

import pytest

from telegram_bot.core.services.session_backend import (
    BackendDispatcher,
    SessionBackend,
)


class _StubBackend:
    """Tiny in-memory backend satisfying the Protocol shape."""

    def __init__(self, name: str) -> None:
        self.name = name

    def is_active(self, channel_key: tuple[int, int | None]) -> bool:
        return False

    def is_processing(self, channel_key: tuple[int, int | None]) -> bool:
        return False


def test_protocol_accepts_stub() -> None:
    backend: SessionBackend = _StubBackend("test")  # type: ignore[assignment]
    assert backend.is_active((1, 2)) is False


def test_dispatcher_picks_backend_by_engine() -> None:
    claude = _StubBackend("claude")
    codex = _StubBackend("codex")
    dispatcher = BackendDispatcher(claude=claude, codex=codex)  # type: ignore[arg-type]

    assert dispatcher.for_engine("claude") is claude  # type: ignore[comparison-overlap]
    assert dispatcher.for_engine("codex") is codex  # type: ignore[comparison-overlap]


def test_dispatcher_unknown_engine_raises() -> None:
    claude = _StubBackend("claude")
    codex = _StubBackend("codex")
    dispatcher = BackendDispatcher(claude=claude, codex=codex)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unknown engine"):
        dispatcher.for_engine("gemini")  # type: ignore[arg-type]

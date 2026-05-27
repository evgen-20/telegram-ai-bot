"""TmuxManager must satisfy SessionBackend."""

from __future__ import annotations

from telegram_bot.core.services.session_backend import SessionBackend
from telegram_bot.core.services.tmux_manager import TmuxManager


def test_tmux_manager_satisfies_protocol() -> None:
    # Runtime Protocol check — uses isinstance against the @runtime_checkable
    # Protocol. This won't catch type mismatches, only missing attributes;
    # mypy --strict in CI catches signature drift.
    assert hasattr(TmuxManager, "start_session")
    assert hasattr(TmuxManager, "send_stream")
    assert hasattr(TmuxManager, "close_buffer")
    assert hasattr(TmuxManager, "cancel")
    assert hasattr(TmuxManager, "is_active")
    assert hasattr(TmuxManager, "is_processing")
    # Protocol-level assignment compile-check is handled by mypy below.
    # Mismatched method signatures would produce per-method incompatibility
    # errors. With Protocol aligned to TmuxManager, this assignment is valid
    # under `warn_unused_ignores = true`, so no `# type: ignore` is needed.
    _check_assignable: type[SessionBackend] = TmuxManager
    del _check_assignable

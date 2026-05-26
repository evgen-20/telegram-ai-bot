"""Tests for CodexDaemonManager."""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from telegram_bot.core.services.codex_daemon import CodexDaemonManager


def _make_socket_at(path: Path) -> socket.socket:
    """Create a listening AF_UNIX socket at `path` (for test only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


@pytest.mark.asyncio
async def test_ensure_running_when_socket_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "app-server-control.sock"
        sock = _make_socket_at(sock_path)
        try:
            mgr = CodexDaemonManager(socket_path=sock_path, start_command=["true"])
            with patch.object(mgr, "_spawn_daemon", new_callable=AsyncMock) as spawn:
                await mgr.ensure_running()
                spawn.assert_not_called()
        finally:
            sock.close()


@pytest.mark.asyncio
async def test_ensure_running_spawns_when_socket_missing() -> None:
    """The manager attempts to spawn a daemon when the socket is missing,
    but tolerates the spawn never producing one — sessions still work via
    ``codex app-server --listen stdio://`` per-session (see
    CodexSessionManager.DEFAULT_PROXY_COMMAND for the rationale).
    """
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "missing.sock"
        mgr = CodexDaemonManager(socket_path=sock_path, start_command=["true"])
        with patch.object(mgr, "_spawn_daemon", new_callable=AsyncMock) as spawn:
            # _wait_for_socket returns False (we never create the socket).
            # ensure_running now logs a warning and returns instead of raising;
            # the daemon is no longer a hard prerequisite.
            await mgr.ensure_running(timeout_sec=0.5)
            spawn.assert_called_once()

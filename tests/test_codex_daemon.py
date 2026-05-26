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
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "missing.sock"
        mgr = CodexDaemonManager(socket_path=sock_path, start_command=["true"])
        with patch.object(mgr, "_spawn_daemon", new_callable=AsyncMock) as spawn:
            # _wait_for_socket returns False (we never create the socket),
            # so ensure_running should raise after the timeout.
            with pytest.raises(RuntimeError, match="daemon did not appear"):
                await mgr.ensure_running(timeout_sec=0.5)
            spawn.assert_called_once()

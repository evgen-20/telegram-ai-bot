"""Manage the lifecycle of the singleton `codex app-server` daemon.

The daemon is one process per `$CODEX_HOME`. We detect it by attempting
to connect to its Unix domain socket; if absent, we start it and wait
up to `timeout_sec` for the socket to appear.

Healthcheck is owned by `CodexSessionManager` (it can issue a cheap RPC
via any active client and treat failures as daemon trouble).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

logger = logging.getLogger("codex_daemon")

DEFAULT_SOCKET_PATH = Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
DEFAULT_START_COMMAND: list[str] = ["codex", "app-server", "daemon", "start"]


class CodexDaemonManager:
    def __init__(
        self,
        *,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        start_command: list[str] = DEFAULT_START_COMMAND,
    ) -> None:
        self._socket_path = socket_path
        self._start_command = start_command

    def is_running(self) -> bool:
        """Heuristic: socket file exists AND a connection succeeds."""
        if not self._socket_path.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(str(self._socket_path))
            sock.close()
            return True
        except OSError:
            return False

    async def ensure_running(self, timeout_sec: float = 10.0) -> None:
        if self.is_running():
            return
        logger.info("starting codex app-server daemon")
        await self._spawn_daemon()
        if not await self._wait_for_socket(timeout_sec):
            raise RuntimeError(
                f"codex daemon did not appear at {self._socket_path} within {timeout_sec}s"
            )

    async def _spawn_daemon(self) -> None:
        # Use DEVNULL so the daemon's own logs don't blast our stderr.
        proc = await asyncio.create_subprocess_exec(
            *self._start_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # `daemon start` should fork and exit promptly; wait briefly.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            logger.warning("daemon start command did not exit in 5s")

    async def _wait_for_socket(self, timeout_sec: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if self.is_running():
                return True
            await asyncio.sleep(0.2)
        return False

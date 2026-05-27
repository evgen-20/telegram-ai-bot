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
        """Best-effort: start the singleton daemon if it isn't already up.

        Historical note: the original spec assumed every session spoke to the
        daemon via ``codex app-server proxy``. In practice that proxy forwards
        raw bytes to a WebSocket-upgraded control socket, which our
        line-based JSON-RPC client cannot use. The session manager now
        spawns ``codex app-server --listen stdio://`` per session, so the
        daemon is not strictly required. We still attempt to start it (cheap
        on standalone installs, no-op when already up) but tolerate failure
        so the bot still works on hosts where the standalone install isn't
        present.
        """
        if self.is_running():
            return
        logger.info("starting codex app-server daemon (best-effort)")
        try:
            await self._spawn_daemon()
        except Exception:
            logger.warning(
                "codex daemon spawn failed; sessions still work via stdio", exc_info=True
            )
            return
        if not await self._wait_for_socket(timeout_sec):
            logger.warning(
                "codex daemon did not appear at %s within %ss; sessions still work via stdio",
                self._socket_path,
                timeout_sec,
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

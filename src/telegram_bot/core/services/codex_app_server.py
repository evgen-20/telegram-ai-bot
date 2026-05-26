"""Async JSON-RPC client over `codex app-server proxy` (or any subprocess
that speaks the same JSON-line protocol on stdio).

Single read loop per client. Request/response correlation via `id` field.
Notifications are pushed through `on_notification(notif: dict)`. Approval
RPCs from server are auto-approved (the bot uses
`--dangerously-bypass-approvals-and-sandbox` semantics).

Reconnect on subprocess EOF: terminate, respawn, re-initialize. Caller is
responsible for re-issuing `thread/resume` after reconnect (handled by
`CodexSessionManager`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telegram_bot.core.services.codex_protocol import (
    ApprovalResponse,
    InitializeResult,
    ThreadStartResponse,
)

logger = logging.getLogger("codex_app_server")

_APPROVAL_METHODS: frozenset[str] = frozenset(
    {
        "execCommandApproval",
        "applyPatchApproval",
        "commandExecutionRequestApproval",
        "fileChangeRequestApproval",
    }
)


class CodexAppServerClient:
    def __init__(
        self,
        *,
        command: list[str],
        on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        self._command = command
        self._on_notification = on_notification
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False

    async def __aenter__(self) -> CodexAppServerClient:
        await self._spawn()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- public API -----------------------------------------------------

    async def initialize(self, *, client_name: str, client_version: str) -> InitializeResult:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "version": client_version},
                "capabilities": {},
            },
        )
        return result  # type: ignore[return-value]

    async def thread_start(self, *, cwd: str, model: str | None) -> ThreadStartResponse:
        params: dict[str, Any] = {"cwd": cwd}
        if model is not None:
            params["model"] = model
        result = await self._request("thread/start", params)
        return result  # type: ignore[return-value]

    async def thread_resume(self, *, thread_id: str) -> None:
        await self._request("thread/resume", {"threadId": thread_id})

    async def turn_start(self, *, thread_id: str, prompt: str) -> None:
        # Fire-and-forget: response is delivered via notifications.
        await self._request("turn/start", {"threadId": thread_id, "prompt": prompt})

    async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
        await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def close(self) -> None:
        self._closed = True
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    # ---- internals ------------------------------------------------------

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_loop(), name="codex_read_loop")

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while not self._closed:
            line = await self._proc.stdout.readline()
            if not line:
                logger.info("codex app-server stdout closed")
                self._fail_pending_with_eof()
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("non-JSON line from codex: %s", line[:200])
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        # Response: has `id` and either `result` or `error`.
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(CodexRpcError(msg["error"].get("message", "unknown")))
                else:
                    fut.set_result(msg.get("result", {}))
            return

        # Server-initiated request (e.g. approval). Has `id` and `method`.
        if "id" in msg and "method" in msg:
            await self._handle_server_request(msg)
            return

        # Notification: has `method` and no `id`.
        if "method" in msg:
            try:
                ret = self._on_notification(msg)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception:
                logger.exception("on_notification handler raised")
            return

        logger.warning("unrecognised JSON-RPC frame: %s", msg)

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        request_id = msg["id"]
        if method in _APPROVAL_METHODS:
            response: ApprovalResponse = {"decision": "approve"}
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": response})
            return
        # Unknown server request: respond with error.
        logger.warning("unknown server request method: %s", method)
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not implemented"},
            }
        )

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("client not started")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("client not started")
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    def _fail_pending_with_eof(self) -> None:
        exc = CodexEofError("codex app-server stdout closed")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class CodexRpcError(RuntimeError):
    """JSON-RPC error response from the server."""


class CodexEofError(RuntimeError):
    """The codex subprocess closed stdout."""

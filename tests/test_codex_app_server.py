"""Tests for CodexAppServerClient using a bash subprocess that prints fixture frames."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.services.codex_app_server import CodexAppServerClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_app_server"


def _stub_command(fixture_name: str) -> list[str]:
    """Bash that cats a fixture file slowly (one line at a time) and exits."""
    path = FIXTURE_DIR / fixture_name
    # Emit one line at a time with a tiny delay so the client has a chance to
    # register pending requests before the matching responses arrive.
    script = (
        f"while IFS= read -r line; do printf '%s\\n' \"$line\"; sleep 0.02;"
        f" done < {path!s}; sleep 0.05"
    )
    return ["bash", "-c", script]


@pytest.mark.asyncio
async def test_initialize_and_thread_start_round_trip() -> None:
    notifications: list[dict[str, Any]] = []

    async def on_notification(notif: dict[str, Any]) -> None:
        notifications.append(notif)

    client = CodexAppServerClient(
        command=_stub_command("initialize_then_thread_start.jsonl"),
        on_notification=on_notification,
    )
    async with client:
        init_result = await client.initialize(client_name="test", client_version="0.0")
        assert init_result["serverInfo"]["name"] == "codex"

        ts_result = await client.thread_start(cwd="/tmp", model=None)
        assert ts_result["threadId"] == "thread-deadbeef"

        # Drain notifications produced after the responses (still inside the
        # context so the read loop and subprocess are alive).
        await asyncio.sleep(0.2)

    methods = [n["method"] for n in notifications]
    assert "turn/started" in methods
    assert "item/agentMessage/delta" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods


def _stub_command_with_stdin_echo(fixture_name: str) -> list[str]:
    """Like _stub_command but also echoes received stdin lines to stderr.

    Emits fixture frames one line at a time (slow) so the client has a
    chance to register pending request futures before responses arrive,
    and concurrently reads its own stdin, echoing each line to stderr so
    the test can assert on what the client wrote back.
    """
    path = FIXTURE_DIR / fixture_name
    script = (
        # Background: echo each stdin line (the client's outbound JSON-RPC
        # frames) to stderr prefixed with "GOT:". We must explicitly dup
        # fd 0 (`<&0`) because bash auto-redirects backgrounded jobs'
        # stdin to /dev/null otherwise.
        '{ while IFS= read -r line; do printf "GOT:%s\\n" "$line" >&2;'
        " done; } <&0 &"
        " reader_pid=$!;"
        # Foreground: emit fixture frames slowly via fd 3 so the
        # foreground loop doesn't steal stdin from the background reader.
        ' while IFS= read -r line <&3; do printf "%s\\n" "$line";'
        f" sleep 0.02; done 3< {path!s};"
        # Give the client time to process the approval request and write
        # its response, then close the reader.
        " sleep 0.3;"
        " kill $reader_pid 2>/dev/null;"
        " wait $reader_pid 2>/dev/null;"
        " :"
    )
    return ["bash", "-c", script]


@pytest.mark.asyncio
async def test_auto_approve_exec_command_approval() -> None:
    """Approval requests from the server get auto-approved."""
    notifications: list[dict[str, Any]] = []

    async def on_notif(n: dict[str, Any]) -> None:
        notifications.append(n)

    client = CodexAppServerClient(
        command=_stub_command_with_stdin_echo("with_approval_request.jsonl"),
        on_notification=on_notif,
    )
    async with client:
        await client.initialize(client_name="t", client_version="0")
        await client.thread_start(cwd="/tmp", model=None)
        # Let the server-initiated approval request be processed and the
        # client's response to be written + echoed back via stderr.
        await asyncio.sleep(0.4)

        # Read what the client wrote back to the subprocess by inspecting
        # the echoed-to-stderr stream.
        assert client._proc is not None
        assert client._proc.stderr is not None
        # The subprocess is still alive here (we are inside `async with`);
        # read whatever has been buffered so far without blocking forever.
        try:
            stderr_bytes = await asyncio.wait_for(client._proc.stderr.read(4096), timeout=0.5)
        except TimeoutError:
            stderr_bytes = b""
    # After the context closes, drain any remaining stderr.
    assert client._proc is not None
    assert client._proc.stderr is not None
    stderr_bytes += await client._proc.stderr.read()
    stderr = stderr_bytes.decode()

    assert '"decision": "approve"' in stderr or '"decision":"approve"' in stderr
    # The approval response must reference the original server request id.
    assert '"id": 100' in stderr or '"id":100' in stderr

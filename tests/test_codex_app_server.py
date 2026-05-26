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

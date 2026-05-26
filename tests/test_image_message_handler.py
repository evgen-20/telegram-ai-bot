"""StreamEvent('image_message', ...) is sent as a Telegram photo."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.handlers.streaming import dispatch_image_event
from telegram_bot.core.services.cc_events import StreamEvent


@pytest.mark.asyncio
async def test_dispatch_image_event_sends_photo(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n" + b"\0" * 6000)  # > 5 KB
    bot = AsyncMock()
    event = StreamEvent("image_message", str(img), "caption")
    await dispatch_image_event(bot=bot, chat_id=-1003, thread_id=42, event=event)
    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == -1003
    assert kwargs["message_thread_id"] == 42
    assert kwargs["caption"] == "caption"

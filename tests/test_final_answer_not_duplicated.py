"""Outside tmux the final answer must reach Telegram exactly once.

`send_stream` returns the turn's final text *and* streams it as the last
`text` event. Sending each `text` event as it arrives therefore delivered
that answer twice — once plain as "progress", once through
`_send_final_response` (rich rendering + topic keyboard). See
`_handle_text_event`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.enums import ChatType

from telegram_bot.core.handlers.streaming import send_streaming_response
from telegram_bot.core.services.cc_events import StreamEvent

_THINKING = "⏳"


def _message(sends: list[str]) -> MagicMock:
    message = MagicMock()
    message.chat.id = -100
    message.chat.type = ChatType.PRIVATE
    message.message_id = 1
    message.bot = None

    async def answer(text: str | None = None, **_: Any) -> MagicMock:
        sends.append(text or "")
        sent = MagicMock()
        sent.message_id = len(sends)
        return sent

    async def answer_rich(rich_message: Any, **_: Any) -> MagicMock:
        sends.append(getattr(rich_message, "html", "<rich>"))
        sent = MagicMock()
        sent.message_id = len(sends)
        return sent

    message.answer = answer
    message.answer_rich = answer_rich
    return message


async def _run(blocks: list[str], response: str) -> list[str]:
    """Stream *blocks* as `text` events, return *response*, report what was sent."""
    sends: list[str] = []
    session_manager = MagicMock()
    session_manager.get_mode.return_value = "free"
    session_manager.override_session = AsyncMock()
    session_manager.record_message_session = MagicMock()
    session_manager.get_current_session_id.return_value = "sid"

    async def send_stream(
        _key: tuple[int, int | None],
        _prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None]],
        **_: Any,
    ) -> str:
        for block in blocks:
            await on_event(StreamEvent("text", block))
        return response

    session_manager.send_stream = send_stream

    await send_streaming_response(
        _message(sends),
        session_manager,
        (-100, None),
        "prompt",
        tmux_manager=None,
        topic_config=None,
    )
    return [text for text in sends if not text.startswith(_THINKING)]


@pytest.mark.asyncio
async def test_sole_text_block_is_not_sent_twice() -> None:
    assert await _run(["Ответ"], "Ответ") == ["Ответ"]


@pytest.mark.asyncio
async def test_intermediate_blocks_still_reach_telegram() -> None:
    """Only the block the final response repeats is dropped."""
    assert await _run(["Сейчас посмотрю", "Ответ"], "Ответ") == ["Сейчас посмотрю", "Ответ"]


@pytest.mark.asyncio
async def test_final_text_differing_from_the_last_block_sends_both() -> None:
    assert await _run(["Комментарий"], "Итог") == ["Комментарий", "Итог"]


@pytest.mark.asyncio
async def test_held_back_block_survives_an_empty_final_response() -> None:
    """With nothing to conclude with, the held-back block is all the user gets."""
    assert await _run(["Только это"], "") == ["Только это"]


@pytest.mark.asyncio
async def test_markdown_table_answer_is_delivered_once_as_rich() -> None:
    table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
    sent = await _run([table], table)
    assert len(sent) == 1
    assert "<table>" in sent[0]

"""Regression tests for the final-review findings on the Antigravity engine."""

from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.services import antigravity as agy
from telegram_bot.core.services.topic_config import TopicSettings
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
A = "aaaaaaaa-0000-4000-8000-000000000000"
B = "bbbbbbbb-0000-4000-8000-000000000000"
PREAMBLE = "## Mode\n" + "Follow the house rules carefully. " * 30  # > 200 chars, shared


def _conversation(home: Path, cid: str, text: str) -> None:
    path = agy.transcript_path(cid, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    step = {
        "step_index": 0,
        "type": "USER_INPUT",
        "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>",
    }
    path.write_text(json.dumps(step) + "\n")


# --- #1 concurrent first messages sharing the mode preamble -------------------


async def test_topics_with_the_same_preamble_bind_their_own_conversation(tmp_path: Path) -> None:
    prompt_a = PREAMBLE + "\nTelegram thread 11\nfirst question"
    prompt_b = PREAMBLE + "\nTelegram thread 22\nsecond question"
    _conversation(tmp_path, A, prompt_a)
    _conversation(tmp_path, B, prompt_b)

    found_a = await agy.locate_conversation(frozenset(), prompt_a, home=tmp_path, timeout_sec=0.2)
    found_b = await agy.locate_conversation(frozenset(), prompt_b, home=tmp_path, timeout_sec=0.2)

    assert (found_a, found_b) == (A, B)


async def test_conversations_claimed_by_other_topics_are_skipped(tmp_path: Path) -> None:
    _conversation(tmp_path, A, "same text")
    _conversation(tmp_path, B, "same text")

    found = await agy.locate_conversation(
        frozenset(), "same text", home=tmp_path, timeout_sec=0.2, exclude=frozenset({A})
    )

    assert found == B


# --- #4 queued input and slash commands ----------------------------------------


def test_a_queued_message_counts_as_delivered() -> None:
    pane = (
        (FIXTURES / "pane_busy.txt")
        .read_text()
        .replace("esc to cancel", "  Press up to edit queued messages")
    )

    assert agy.is_input_queued(pane)
    assert not agy.is_input_queued((FIXTURES / "pane_busy.txt").read_text())


def test_the_usage_screen_blocks_input() -> None:
    pane = (
        "  Weekly Limit Remaining\n    Quota available\n"
        "  ↑/↓ Scroll · pgup/pgdown Page · ctrl+end Bottom · ctrl+home Top · esc Close\n"
    )

    assert agy.is_modal_present(pane)
    assert not agy.is_prompt_ready(pane)


# --- #5 "Default" model must not inherit another engine's legacy model -----------


def test_antigravity_ignores_the_legacy_single_model_field(tmp_path: Path) -> None:
    settings = TopicSettings(
        name="t",
        type="project",
        mode="free",
        cwd=None,
        mcp_config=None,
        engine="antigravity",
        model="claude-opus-4-1",
    )

    runtime = resolve_topic_runtime_config(settings, BotDefaults(cwd=tmp_path, mcp_config=None))

    assert runtime.model is None


def test_claude_still_uses_the_legacy_single_model_field(tmp_path: Path) -> None:
    settings = TopicSettings(
        name="t",
        type="project",
        mode="free",
        cwd=None,
        mcp_config=None,
        engine="claude",
        model="opus",
    )

    runtime = resolve_topic_runtime_config(settings, BotDefaults(cwd=tmp_path, mcp_config=None))

    assert runtime.model == "opus"


# --- #7 image de-duplication is per turn -----------------------------------------


async def test_the_same_image_path_in_a_later_turn_is_posted_again(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from telegram_bot.core.handlers.streaming import send_streaming_response
    from telegram_bot.core.services.cc_events import StreamEvent

    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def send_stream(channel_key, prompt, on_event, **kwargs):
        for turn in ("t1", "t2"):
            await on_event(StreamEvent("turn_start", "", turn_id=turn))
            await on_event(StreamEvent("image_message", str(image), turn_id=turn))
            await on_event(StreamEvent("image_message", str(image), turn_id=turn))
            await on_event(StreamEvent("turn_end", "", turn_id=turn))
        return "done"

    session_manager = MagicMock()
    session_manager.send_stream = send_stream
    session_manager.get_current_session_id.return_value = None
    session_manager.get_session_id.return_value = None
    message = MagicMock()
    message.chat.id = -100
    message.bot = AsyncMock()
    message.answer = AsyncMock(return_value=MagicMock(message_id=1))

    await send_streaming_response(message, session_manager, (-100, None), "draw twice")

    assert message.bot.send_photo.await_count == 2


# --- #4 delivery acknowledgement in the tmux send path ---------------------------


async def _send_with_pane(monkeypatch, tmp_path: Path, pane: str, prompt: str) -> bool:
    from telegram_bot.core.services import tmux_manager as tm_module
    from telegram_bot.core.services.tmux_manager import TmuxManager
    from telegram_bot.core.services.tmux_state import TmuxSessionState

    transcript = agy.transcript_path(A, home=tmp_path)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("")
    sent: list[str] = []

    async def fake_capture(name: str) -> str:
        return pane

    async def fake_send(name: str, text: str, *, submit_enter: bool = True) -> None:
        sent.append(text)

    monkeypatch.setattr(tm_module, "capture_pane", fake_capture)
    monkeypatch.setattr(tm_module, "send_text_to_tmux", fake_send)
    monkeypatch.setattr(tm_module, "_AGY_DELIVERY_ACK_SEC", 0.3)
    manager = TmuxManager(sessions_dir=tmp_path / "sessions")
    alerts: list[str] = []

    async def fake_alert(*args, reason: str = "", **kwargs) -> None:
        alerts.append(reason)

    monkeypatch.setattr(manager, "_send_modal_alert", fake_alert)
    state = TmuxSessionState(
        session_name="cc-x",
        session_dir=str(tmp_path),
        session_id=A,
        mode="free",
        cwd=str(tmp_path),
        mcp_config="",
        chat_id=-100,
        offset=0,
        runner_version="antigravity-tui-v1",
        provider="antigravity",
        transcript_path=str(transcript),
    )
    delivered = await manager._safe_send_antigravity((-100, 1), state, prompt)
    assert sent == [prompt]
    return delivered and not alerts


async def test_input_queued_behind_a_running_turn_is_delivered(tmp_path: Path, monkeypatch) -> None:
    busy = (FIXTURES / "pane_busy.txt").read_text()
    queued = busy.replace("esc to cancel", "  Press up to edit queued messages")

    assert await _send_with_pane(monkeypatch, tmp_path, queued, "follow-up")


async def test_agy_slash_commands_are_not_waited_on(tmp_path: Path, monkeypatch) -> None:
    idle = (FIXTURES / "pane_idle.txt").read_text()

    assert await _send_with_pane(monkeypatch, tmp_path, idle, "/usage")


async def test_unacknowledged_input_still_alerts(tmp_path: Path, monkeypatch) -> None:
    idle = (FIXTURES / "pane_idle.txt").read_text()

    assert not await _send_with_pane(monkeypatch, tmp_path, idle, "vanished")

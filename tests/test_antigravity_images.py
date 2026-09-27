from __future__ import annotations

import asyncio
import json
from pathlib import Path

from telegram_bot.core.services import antigravity as agy
from telegram_bot.core.services.cc_events import StreamEvent

CID = "11111111-2222-4333-8444-555555555555"
FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def _write_steps(home: Path, steps: list[dict[str, object]]) -> Path:
    path = agy.transcript_path(CID, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(step) + "\n" for step in steps))
    return path


def _media(index: int, uri: str) -> dict[str, object]:
    return {
        "step_index": index,
        "type": "GENERIC",
        "status": "DONE",
        "content": "Generated image is saved at somewhere.",
        "media": [{"mime_type": "image/jpeg", "uri": uri}],
    }


def test_images_are_found_for_the_generate_image_steps(tmp_path: Path) -> None:
    brain = agy.brain_dir(CID, home=tmp_path)
    _write_steps(
        tmp_path,
        [
            {"step_index": 0, "type": "USER_INPUT", "content": "x"},
            _media(2, f"file://{brain}/cat_1.jpg"),
            _media(4, f"{brain}/.tempmediaStorage/viewed.png"),
        ],
    )

    assert agy.find_generated_images(CID, steps=[2], home=tmp_path) == [brain / "cat_1.jpg"]


def test_image_paths_outside_the_conversation_are_dropped(tmp_path: Path) -> None:
    _write_steps(tmp_path, [_media(2, "file:///etc/hosts")])

    assert agy.find_generated_images(CID, steps=[2], home=tmp_path) == []


def test_missing_transcript_yields_nothing(tmp_path: Path) -> None:
    assert agy.find_generated_images(CID, steps=[2], home=tmp_path) == []


def test_malformed_conversation_id_yields_nothing(tmp_path: Path) -> None:
    assert agy.find_generated_images("../../etc", steps=[2], home=tmp_path) == []


async def test_subprocess_run_delivers_the_generated_image(tmp_path: Path, monkeypatch) -> None:
    from telegram_bot.core.config import Settings
    from telegram_bot.core.services.claude import SessionManager

    monkeypatch.setattr(agy, "AGY_HOME", tmp_path)
    brain = agy.brain_dir(CID, home=tmp_path)
    _write_steps(tmp_path, [_media(2, f"file://{brain}/watercolor_kitten_1.jpg")])
    settings = Settings(_env_file=None, telegram_bot_token="x", project_root=str(tmp_path))
    manager = SessionManager(settings)
    process = await asyncio.create_subprocess_exec(
        "cat", str(FIXTURES / "exec_generate_image.ndjson"), stdout=asyncio.subprocess.PIPE
    )
    events: list[StreamEvent] = []

    await manager._read_stream(process, events.append, provider="antigravity")
    await process.wait()

    images = [e.content for e in events if e.type == "image_message"]
    assert images == [str(brain / "watercolor_kitten_1.jpg")]


def test_first_message_preamble_tells_the_agent_images_arrive_on_their_own() -> None:
    assert "generate_image" in agy.PROMPT_NOTE
    assert agy.PROMPT_NOTE.strip()


async def test_the_same_image_is_posted_once_per_stream(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    from telegram_bot.core.handlers.streaming import send_streaming_response

    image = tmp_path / "cat.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")

    async def send_stream(channel_key, prompt, on_event, **kwargs):
        for _ in range(2):
            await on_event(StreamEvent("image_message", str(image)))
        return "done"

    session_manager = MagicMock()
    session_manager.send_stream = send_stream
    session_manager.get_current_session_id.return_value = None
    session_manager.get_session_id.return_value = None

    message = MagicMock()
    message.chat.id = -100
    message.bot = AsyncMock()
    message.answer = AsyncMock(return_value=MagicMock(message_id=1))

    await send_streaming_response(message, session_manager, (-100, None), "draw")

    assert message.bot.send_photo.await_count == 1

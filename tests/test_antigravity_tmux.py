"""tmux integration for the Antigravity engine, driven by a fake agy TUI.

A dedicated tmux server (``TMUX_TMPDIR`` under ``tmp_path``) runs
``fixtures/antigravity/fake_agy.py`` as the agy binary, so the real
``TmuxManager`` paths — spawn, trust auto-confirm, input probe, delivery,
conversation discovery, transcript tail, resume — run end to end offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from telegram_bot.core.config import Settings
from telegram_bot.core.messages import t
from telegram_bot.core.services import antigravity
from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.tmux_manager import TmuxManager

FAKE = Path(__file__).parent / "fixtures" / "antigravity" / "fake_agy.py"
KEY = (-100123, 77)

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


@pytest.fixture
def agy_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "agyhome"
    home.mkdir()
    binary = home / "agy"
    shutil.copy(FAKE, binary)
    binary.chmod(0o755)
    monkeypatch.setenv("TELEGRAM_AGY_BIN", str(binary))
    monkeypatch.setattr(antigravity, "AGY_HOME", home)
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.delenv("TMUX", raising=False)
    yield home
    subprocess.run(
        ["tmux", "kill-server"],
        env={**os.environ, "TMUX_TMPDIR": str(tmux_dir)},
        capture_output=True,
        check=False,
    )


@pytest.fixture
def managers(tmp_path: Path, agy_home: Path) -> tuple[TmuxManager, SessionManager, Path]:
    work = tmp_path / "work"
    work.mkdir()
    settings = Settings(_env_file=None, telegram_bot_token="x", project_root=str(tmp_path))
    return TmuxManager(sessions_dir=tmp_path / "sessions"), SessionManager(settings), work


async def _start(tmux: TmuxManager, sm: SessionManager, work: Path, resume: str | None = None):
    await tmux.start_session(
        KEY,
        mode="free",
        cwd=str(work),
        mcp_config="",
        chat_id=KEY[0],
        session_manager=sm,
        resume_session_id=resume,
        provider="antigravity",
    )


async def _turn(tmux: TmuxManager, prompt: str, *, timeout: float = 20.0) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    done = asyncio.Event()

    def on_event(event: StreamEvent) -> None:
        events.append(event)
        if event.type == "turn_end":
            done.set()

    task = asyncio.create_task(tmux.send_stream(KEY, prompt, on_event))
    try:
        await asyncio.wait_for(done.wait(), timeout)
    finally:
        await tmux.cancel(KEY)
        await asyncio.wait_for(task, 10)
    return events


async def test_start_confirms_trust_and_registers_an_antigravity_session(
    managers, agy_home: Path
) -> None:
    tmux, sm, work = managers

    await _start(tmux, sm, work)

    state = tmux._sessions[KEY]
    assert tmux.is_active(KEY)
    assert state.provider == "antigravity"
    assert state.runner_version == "antigravity-tui-v1"
    assert state.session_id is None
    assert (agy_home / "trusted").exists()


async def test_first_message_binds_the_conversation_and_streams_the_answer(
    managers, agy_home: Path
) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)

    events = await _turn(tmux, "hello there")

    state = tmux._sessions[KEY]
    assert state.session_id is not None
    assert antigravity.is_conversation_id(state.session_id)
    assert state.transcript_path == str(antigravity.transcript_path(state.session_id))
    finals = [e.content for e in events if e.type == "result_message"]
    assert finals == ["echo: hello there"]
    first_input = json.loads(Path(state.transcript_path).read_text().splitlines()[0])
    # The mode prompt and Telegram context ride along with the first message only.
    assert first_input["content"].count("hello there") == 1
    assert len(first_input["content"]) > len("<USER_REQUEST>\nhello there\n</USER_REQUEST>")
    assert antigravity.PROMPT_NOTE.strip() in first_input["content"]


async def test_follow_up_is_delivered_without_the_preamble(managers) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)
    await _turn(tmux, "first")

    events = await _turn(tmux, "second")

    state = tmux._sessions[KEY]
    inputs = [
        json.loads(line)
        for line in Path(state.transcript_path or "").read_text().splitlines()
        if '"USER_INPUT"' in line
    ]
    assert inputs[-1]["content"] == "<USER_REQUEST>\nsecond\n</USER_REQUEST>"
    assert [e.content for e in events if e.type == "result_message"] == ["echo: second"]


async def test_generated_image_reaches_the_chat(managers, agy_home: Path) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)

    events = await _turn(tmux, "draw IMAGE please")

    images = [e.content for e in events if e.type == "image_message"]
    assert len(images) == 1
    assert images[0].startswith(str(agy_home / "brain"))


async def test_clear_context_starts_a_fresh_conversation(managers) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)
    await _turn(tmux, "one")
    old = tmux._sessions[KEY].session_id

    assert await tmux.clear_context(KEY, sm)
    await _turn(tmux, "two")

    new = tmux._sessions[KEY].session_id
    assert new is not None
    assert new != old


async def test_resume_continues_the_same_transcript(managers) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)
    await _turn(tmux, "one")
    cid = tmux._sessions[KEY].session_id
    assert cid is not None
    await tmux.kill(KEY)

    await _start(tmux, sm, work, resume=cid)
    events = await _turn(tmux, "again")

    state = tmux._sessions[KEY]
    assert state.session_id == cid
    assert [e.content for e in events if e.type == "result_message"] == ["echo: again"]


async def test_state_round_trips_through_restore(managers, tmp_path: Path) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)
    await _turn(tmux, "persist me")
    cid = tmux._sessions[KEY].session_id

    restored = TmuxManager(sessions_dir=tmp_path / "sessions")
    restored.restore_all(sm)

    assert KEY in restored._sessions
    assert restored._sessions[KEY].provider == "antigravity"
    assert restored._sessions[KEY].session_id == cid


async def test_first_message_after_a_bot_restart_still_carries_the_preamble(
    managers, tmp_path: Path
) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)

    restarted = TmuxManager(sessions_dir=tmp_path / "sessions")
    restarted.restore_all(sm)
    await _turn(restarted, "after restart")

    state = restarted._sessions[KEY]
    first_input = json.loads(Path(state.transcript_path or "").read_text().splitlines()[0])
    assert antigravity.PROMPT_NOTE.strip() in first_input["content"]


async def test_failed_discovery_resets_the_pane_so_the_next_message_works(
    managers, monkeypatch
) -> None:
    tmux, sm, work = managers
    await _start(tmux, sm, work)
    real_locate = antigravity.locate_conversation

    async def never_found(*args, **kwargs):
        return None

    monkeypatch.setattr(antigravity, "locate_conversation", never_found)
    events: list[StreamEvent] = []
    await tmux.send_stream(KEY, "lost one", events.append)

    assert [e.content for e in events if e.type == "result_message"] == [
        t("ui.antigravity_discovery_failed")
    ]
    assert not tmux.is_active(KEY)

    monkeypatch.setattr(antigravity, "locate_conversation", real_locate)
    await _start(tmux, sm, work)
    events = await _turn(tmux, "second try")
    assert [e.content for e in events if e.type == "result_message"] == ["echo: second try"]

from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.services import antigravity as agy
from telegram_bot.core.services.providers import ANTIGRAVITY_ADAPTER

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"


def _pane(name: str) -> str:
    return (FIXTURES / f"pane_{name}.txt").read_text()


def test_idle_pane_is_ready() -> None:
    assert agy.is_prompt_ready(_pane("idle"))
    assert agy.is_prompt_ready(_pane("idle_after"))


def test_busy_pane_is_not_ready() -> None:
    assert not agy.is_prompt_ready(_pane("busy"))


def test_trust_dialog_is_detected_and_is_not_ready() -> None:
    pane = _pane("trust")

    assert agy.is_trust_dialog(pane)
    assert agy.is_modal_present(pane)
    assert not agy.is_prompt_ready(pane)


def test_old_dialog_in_scrollback_is_ignored() -> None:
    pane = _pane("trust") + "\n" + _pane("idle_after")

    assert not agy.is_trust_dialog(pane)
    assert not agy.is_modal_present(pane)
    assert agy.is_prompt_ready(pane)


def test_idle_and_busy_panes_have_no_modal() -> None:
    assert not agy.is_modal_present(_pane("idle"))
    assert not agy.is_modal_present(_pane("busy"))


def test_adapter_exposes_the_tui_detectors() -> None:
    assert ANTIGRAVITY_ADAPTER.is_prompt_ready(_pane("idle"))
    assert ANTIGRAVITY_ADAPTER.is_modal_present(_pane("trust"))


# --- conversation discovery --------------------------------------------------

OLD = "aaaaaaaa-0000-4000-8000-000000000000"
MINE = "bbbbbbbb-0000-4000-8000-000000000000"
FOREIGN = "cccccccc-0000-4000-8000-000000000000"


def _write_conversation(home: Path, cid: str, *user_texts: str) -> Path:
    path = agy.transcript_path(cid, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for index, text in enumerate(user_texts):
            f.write(
                json.dumps(
                    {
                        "step_index": index,
                        "source": "USER_EXPLICIT",
                        "type": "USER_INPUT",
                        "status": "DONE",
                        "content": (
                            f"<USER_REQUEST>\n{text}\n</USER_REQUEST>\n"
                            "<ADDITIONAL_METADATA>\nThe current local time is: "
                            "2026-09-27T22:32:18Z.\n</ADDITIONAL_METADATA>"
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return path


def test_snapshot_lists_existing_conversations(tmp_path: Path) -> None:
    _write_conversation(tmp_path, OLD, "old")

    assert agy.snapshot_conversations(home=tmp_path) == frozenset({OLD})
    assert agy.snapshot_conversations(home=tmp_path / "missing") == frozenset()


async def test_locate_picks_the_new_conversation_with_this_prompt(tmp_path: Path) -> None:
    _write_conversation(tmp_path, OLD, "Сделай   отчёт\nпо логам")
    snapshot = agy.snapshot_conversations(home=tmp_path)
    _write_conversation(tmp_path, FOREIGN, "совсем другой запрос")
    _write_conversation(tmp_path, MINE, "Сделай отчёт по логам")

    found = await agy.locate_conversation(
        snapshot, "Сделай   отчёт\nпо логам", home=tmp_path, timeout_sec=1.0
    )

    assert found == MINE


async def test_locate_ignores_directories_without_a_transcript(tmp_path: Path) -> None:
    (tmp_path / "brain" / MINE).mkdir(parents=True)
    (tmp_path / "brain" / "not-a-uuid").mkdir(parents=True)

    found = await agy.locate_conversation(
        frozenset(), "hello", home=tmp_path, timeout_sec=0.3, poll_sec=0.05
    )

    assert found is None


async def test_locate_waits_for_the_transcript_to_appear(tmp_path: Path) -> None:
    import asyncio

    async def late_write() -> None:
        await asyncio.sleep(0.2)
        _write_conversation(tmp_path, MINE, "hello there")

    writer = asyncio.create_task(late_write())
    found = await agy.locate_conversation(
        frozenset(), "hello there", home=tmp_path, timeout_sec=2.0, poll_sec=0.05
    )
    await writer

    assert found == MINE


def test_user_input_ack_only_counts_lines_after_the_offset(tmp_path: Path) -> None:
    path = _write_conversation(tmp_path, MINE, "первый")
    offset = path.stat().st_size

    assert not agy.transcript_has_user_input(path, offset, "первый")
    _write_conversation(tmp_path, MINE, "второй вопрос")
    assert agy.transcript_has_user_input(path, offset, "второй   вопрос")
    assert not agy.transcript_has_user_input(path, offset, "третий")
    assert not agy.transcript_has_user_input(tmp_path / "missing.jsonl", 0, "x")


def test_input_bar_content_reads_the_line_between_separators() -> None:
    assert agy.input_bar_content(_pane("idle")) == ""
    assert agy.input_bar_content(_pane("idle").replace("\n>\n", "\n> .\n", 1)) == "."
    assert agy.input_bar_content(_pane("trust")) is None

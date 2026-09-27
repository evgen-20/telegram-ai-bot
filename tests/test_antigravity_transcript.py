from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.services.antigravity import (
    AntigravityTranscriptParser,
    tool_status_line,
)
from telegram_bot.core.services.cc_events import StreamEvent

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
CID = "11111111-2222-4333-8444-555555555555"
BRAIN = Path("/home/user/.gemini/antigravity-cli/brain") / CID


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def _parse(parser: AntigravityTranscriptParser, lines: list[str]) -> list[StreamEvent]:
    return [event for line in lines for event in parser.parse(line).events]


def _brief(events: list[StreamEvent]) -> list[tuple[str, str, str | None]]:
    return [(e.type, e.content, e.turn_id) for e in events]


def _step(index: int, source: str, kind: str, **fields: object) -> str:
    return json.dumps(
        {"step_index": index, "source": source, "type": kind, "status": "DONE", **fields}
    )


def test_multi_step_turn_streams_interim_text_statuses_and_one_final_answer() -> None:
    events = _parse(
        AntigravityTranscriptParser(brain_root=BRAIN), _lines("transcript_session.jsonl")[:8]
    )

    assert events[0] == StreamEvent("turn_start", "", turn_id="agy-0")
    assert events[1] == StreamEvent(
        "status", tool_status_line("run_command", {"CommandLine": "sleep 4"}), turn_id="agy-0"
    )
    assert ("text", "1. Команда `sleep 4` выполнена успешно.", "agy-0") in _brief(events)
    finals = [e for e in events if e.type == "result_message"]
    assert len(finals) == 1
    assert finals[0].content.startswith("3. Команда `sleep 4 && echo готово` выполнена.")
    assert events[-1] == StreamEvent("turn_end", "", turn_id="agy-0")


def test_system_message_inside_a_turn_does_not_open_another_turn() -> None:
    events = _parse(
        AntigravityTranscriptParser(brain_root=BRAIN), _lines("transcript_session.jsonl")[8:11]
    )

    assert _brief(events) == [
        ("turn_start", "", "agy-8"),
        ("result_message", "Sun Sep 27 10:22:10 PM UTC 2026", "agy-8"),
        ("turn_end", "", "agy-8"),
    ]


def test_background_task_continuation_is_delivered_as_its_own_turn() -> None:
    events = _parse(
        AntigravityTranscriptParser(brain_root=BRAIN), _lines("transcript_session.jsonl")[11:18]
    )

    brief = _brief(events)
    assert (
        "result_message",
        "Команда `sleep 40` запущена в фоновом режиме. Ожидаю её завершения.",
        "agy-11",
    ) in brief
    assert brief[-3:] == [
        ("turn_start", "", "agy-17"),
        ("result_message", "Готово.", "agy-17"),
        ("turn_end", "", "agy-17"),
    ]


def test_an_ignored_system_notice_does_not_open_a_turn() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)
    lines = _lines("transcript_session.jsonl")

    events = _parse(parser, [*lines[8:11], lines[16]])

    assert events[-1] == StreamEvent("turn_end", "", turn_id="agy-8")
    assert parser.current_turn_id is None


def test_cancelled_turn_is_closed_by_the_next_user_input() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)
    _parse(parser, _lines("transcript_session.jsonl"))
    assert parser.current_turn_id == "agy-18"

    events = _parse(
        parser,
        [
            _step(
                19, "USER_EXPLICIT", "USER_INPUT", content="<USER_REQUEST>\nещё\n</USER_REQUEST>"
            ),
            _step(20, "MODEL", "PLANNER_RESPONSE", content="ок"),
        ],
    )

    assert _brief(events) == [
        ("turn_end", "", "agy-18"),
        ("turn_start", "", "agy-19"),
        ("result_message", "ок", "agy-19"),
        ("turn_end", "", "agy-19"),
    ]
    assert parser.current_turn_id is None


def test_generated_image_inside_the_conversation_is_sent() -> None:
    events = _parse(AntigravityTranscriptParser(brain_root=BRAIN), _lines("transcript_image.jsonl"))

    images = [e for e in events if e.type == "image_message"]
    assert len(images) == 1
    assert images[0].content.startswith(str(BRAIN) + "/")
    assert images[0].content.endswith(".jpg")


def test_image_outside_the_conversation_directory_is_dropped() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)
    lines = [
        _step(0, "USER_EXPLICIT", "USER_INPUT", content="x"),
        _step(
            1, "MODEL", "GENERIC", media=[{"mime_type": "image/png", "uri": "file:///etc/passwd"}]
        ),
        _step(
            2,
            "MODEL",
            "GENERIC",
            media=[{"mime_type": "image/png", "uri": f"file://{BRAIN}/../../other/x.png"}],
        ),
    ]

    assert [e for e in _parse(parser, lines) if e.type == "image_message"] == []


def test_bare_path_media_uri_is_accepted() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)
    lines = [
        _step(0, "USER_EXPLICIT", "USER_INPUT", content="x"),
        _step(1, "MODEL", "GENERIC", media=[{"mime_type": "image/png", "uri": f"{BRAIN}/a.png"}]),
        _step(2, "MODEL", "GENERIC", media=[{"mime_type": "text/plain", "uri": f"{BRAIN}/a.txt"}]),
    ]

    images = [e for e in _parse(parser, lines) if e.type == "image_message"]
    assert [e.content for e in images] == [f"{BRAIN}/a.png"]


def test_response_without_an_open_turn_opens_one() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)

    events = parser.parse(_step(5, "MODEL", "PLANNER_RESPONSE", content="hi")).events

    assert _brief(events) == [
        ("turn_start", "", "agy-5"),
        ("result_message", "hi", "agy-5"),
        ("turn_end", "", "agy-5"),
    ]


def test_malformed_and_unknown_lines_produce_nothing() -> None:
    parser = AntigravityTranscriptParser(brain_root=BRAIN)

    assert parser.parse("{broken").events == []
    assert parser.parse(json.dumps(["list"])).events == []
    assert parser.parse(_step(1, "MODEL", "CHECKPOINT")).events == []


def test_turn_boundary_is_a_user_input() -> None:
    lines = _lines("transcript_session.jsonl")

    assert AntigravityTranscriptParser.is_turn_boundary(lines[0])
    assert not AntigravityTranscriptParser.is_turn_boundary(lines[1])
    assert not AntigravityTranscriptParser.is_turn_boundary("garbage")

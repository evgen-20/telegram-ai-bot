from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.messages import t
from telegram_bot.core.services.antigravity import AntigravityExecParser, tool_status_line
from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.providers import ANTIGRAVITY_ADAPTER

FIXTURES = Path(__file__).parent / "fixtures" / "antigravity"
CID = "11111111-2222-4333-8444-555555555555"


def _run(name: str) -> tuple[list[StreamEvent], str | None, AntigravityExecParser]:
    parser = AntigravityExecParser()
    events: list[StreamEvent] = []
    session_id: str | None = None
    for line in (FIXTURES / name).read_text().splitlines():
        parsed = parser.parse(line)
        events.extend(parsed.events)
        session_id = parsed.session_id or session_id
    return events, session_id, parser


def _step(**fields: object) -> str:
    return json.dumps({"event": "step_update", "step_update": {"conversation_id": CID, **fields}})


def test_init_yields_the_conversation_id() -> None:
    _events, session_id, _ = _run("exec_streamed_answer.ndjson")

    assert session_id == CID


def test_streamed_final_answer_is_delivered_once_as_the_result() -> None:
    events, _, _ = _run("exec_streamed_answer.ndjson")

    assert [(e.type, e.content) for e in events] == [("result", "Привет\n")]


def test_tool_steps_become_status_lines_and_final_text_is_not_duplicated() -> None:
    events, _, _ = _run("exec_tools.ndjson")

    statuses = [e.content for e in events if e.type == "status"]
    assert statuses == [
        tool_status_line("run_command", {"CommandLine": "cat a.txt"}),
        tool_status_line("view_file", {"AbsolutePath": "/work/project/a.txt"}),
    ]
    assert [e for e in events if e.type == "text"] == []
    assert events[-1] == StreamEvent("result", "hello\n")


def test_interim_text_before_a_tool_is_flushed_as_text() -> None:
    parser = AntigravityExecParser()
    lines = [
        _step(step_index=1, state="ACTIVE", step_type="agent_response", text_delta="Смотрю "),
        _step(step_index=1, state="DONE", step_type="agent_response", text_delta="файлы"),
        _step(
            step_index=2,
            state="ACTIVE",
            step_type="tool",
            tool_name="run_command",
            tool_info={"name": "run_command", "parameters": {"CommandLine": "ls"}},
        ),
    ]
    events = [event for line in lines for event in parser.parse(line).events]

    assert events[0] == StreamEvent("text", "Смотрю файлы")
    assert events[1].type == "status"


def test_generate_image_is_announced_and_remembered_for_lookup() -> None:
    events, _, parser = _run("exec_generate_image.ndjson")

    assert tool_status_line("generate_image", {}) in [e.content for e in events]
    assert parser.pending_images() == [2]
    assert parser.pending_images() == []


def test_location_error_gets_an_account_hint() -> None:
    events, _, _ = _run("exec_location_error.ndjson")

    assert events == [StreamEvent("result", t("ui.antigravity_location_error"))]


def test_auth_error_points_to_sign_in() -> None:
    parser = AntigravityExecParser()
    line = json.dumps(
        {
            "event": "result",
            "result": {
                "status": "ERROR",
                "response": "",
                "error": "error getting token source: You are not logged into Antigravity.",
            },
        }
    )

    assert parser.parse(line).events == [StreamEvent("result", t("ui.antigravity_auth_error"))]


def test_agy_error_line_without_result_is_reported_on_finish() -> None:
    parser = AntigravityExecParser()
    parser.parse('AGY_ERROR: {"short_error":"UNAVAILABLE: backend down","retryable":true}')

    assert parser.finish() == [
        StreamEvent("result", t("ui.antigravity_error", error="UNAVAILABLE: backend down"))
    ]


def test_finish_after_a_result_adds_nothing() -> None:
    _events, _, parser = _run("exec_streamed_answer.ndjson")

    assert parser.finish() == []


def test_garbage_and_unknown_events_are_ignored() -> None:
    parser = AntigravityExecParser()

    assert parser.parse("not json").events == []
    assert parser.parse('{"event":"telemetry","telemetry":{}}').events == []
    assert parser.parse(_step(step_index=3, state="DONE", step_type="checkpoint")).events == []


def test_tool_status_lines_reuse_claude_labels() -> None:
    from telegram_bot.core.services.cc_events import _tool_status

    assert tool_status_line("run_command", {"CommandLine": "ls -la"}) == _tool_status(
        "Bash", {"command": "ls -la"}
    )
    assert tool_status_line("view_file", {"AbsolutePath": "/w/a.py"}) == _tool_status(
        "Read", {"file_path": "/w/a.py"}
    )
    assert tool_status_line("write_to_file", {"TargetFile": "/w/b.py"}) == _tool_status(
        "Write", {"file_path": "/w/b.py"}
    )
    assert tool_status_line("replace_file_content", {"TargetFile": "/w/b.py"}) == _tool_status(
        "Edit", {"file_path": "/w/b.py"}
    )
    assert tool_status_line("browser_click_element", {}) == "⏳ browser_click_element..."


def test_adapter_parse_exec_event_is_stateless_per_line() -> None:
    line = json.dumps({"event": "init", "conversation_id": CID, "init": {}})

    assert ANTIGRAVITY_ADAPTER.parse_exec_event(line).session_id == CID

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


# --- subprocess execution (claude.py branch) --------------------------------


def _session_manager(tmp_path: Path):
    from telegram_bot.core.config import Settings
    from telegram_bot.core.services.claude import SessionManager

    settings = Settings(_env_file=None, telegram_bot_token="x", project_root=str(tmp_path))
    return SessionManager(settings)


def _agy_session(**overrides: object):
    from telegram_bot.core.services.claude import SessionData

    session = SessionData(
        engine="antigravity", cwd="/work/project", chat_id=-100, thread_id=7, mode="free"
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def test_exec_command_for_a_new_conversation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.services.antigravity.antigravity_binary", lambda: "/b/agy"
    )
    manager = _session_manager(tmp_path)

    cmd = manager._build_exec_command("привет", _agy_session())

    assert cmd.argv[0] == "/b/agy"
    prompt = cmd.argv[cmd.argv.index("-p") + 1]
    assert prompt.endswith("привет")
    assert prompt != "привет"  # mode prompt and Telegram context are prepended
    assert "--conversation" not in cmd.argv
    assert cmd.cwd == "/work/project"
    assert cmd.stdin_text == ""
    assert cmd.env is not None
    assert cmd.env["TELEGRAM_CHAT_ID"] == "-100"
    assert cmd.env["TELEGRAM_THREAD_ID"] == "7"


def test_exec_command_resumes_with_the_bare_prompt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.services.antigravity.antigravity_binary", lambda: "/b/agy"
    )
    manager = _session_manager(tmp_path)

    cmd = manager._build_exec_command(
        "ещё", _agy_session(session_id=CID, model="gemini-3.1-pro-high")
    )

    assert cmd.argv[cmd.argv.index("-p") + 1] == "ещё"
    assert cmd.argv[cmd.argv.index("--conversation") + 1] == CID
    assert cmd.argv[cmd.argv.index("--model") + 1] == "gemini-3.1-pro-high"


async def _stream_fixture(tmp_path: Path, name: str) -> tuple[list[StreamEvent], str, str | None]:
    import asyncio

    manager = _session_manager(tmp_path)
    process = await asyncio.create_subprocess_exec(
        "cat",
        str(FIXTURES / name),
        stdout=asyncio.subprocess.PIPE,
    )
    events: list[StreamEvent] = []
    result, session_id = await manager._read_stream(process, events.append, provider="antigravity")
    await process.wait()
    return events, result, session_id


async def test_read_stream_drives_the_antigravity_parser(tmp_path: Path) -> None:
    events, result, session_id = await _stream_fixture(tmp_path, "exec_tools.ndjson")

    assert result == "hello\n"
    assert session_id == CID
    assert [e.type for e in events] == ["status", "status"]


async def test_read_stream_reports_errors_as_the_result(tmp_path: Path) -> None:
    _events, result, _ = await _stream_fixture(tmp_path, "exec_location_error.ndjson")

    assert result == t("ui.antigravity_location_error")


def test_stderr_agy_error_is_turned_into_an_answer() -> None:
    from telegram_bot.core.services.antigravity import error_from_stderr

    stderr = 'noise\nAGY_ERROR: {"short_error":"You are not logged into Antigravity."}\n'

    assert error_from_stderr(stderr) == t("ui.antigravity_auth_error")
    assert error_from_stderr("plain noise") is None

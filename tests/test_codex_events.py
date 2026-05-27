"""Coverage of codex notification -> StreamEvent mapping."""

from __future__ import annotations

from telegram_bot.core.services.codex_events import parse_codex_notification


def _notif(method: str, **params: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def test_agent_message_delta_suppressed() -> None:
    # Codex deltas are per-glyph; we drop them and rely on item/completed
    # (final_answer) to deliver the full text once. Otherwise Telegram
    # receives one message per character.
    events = parse_codex_notification(
        _notif("item/agentMessage/delta", threadId="t", itemId="i", delta="hi ")
    )
    assert events == []


def test_agent_message_final_emits_result_message() -> None:
    item = {
        "id": "i",
        "type": "agentMessage",
        "text": "done.",
        "phase": "final_answer",
        "status": "completed",
    }
    events = parse_codex_notification(_notif("item/completed", threadId="t", item=item))
    assert [(e.type, e.content) for e in events] == [("result_message", "done.")]


def test_command_execution_started_emits_status() -> None:
    item = {
        "id": "i",
        "type": "commandExecution",
        "command": "ls",
        "status": "in_progress",
    }
    events = parse_codex_notification(_notif("item/started", threadId="t", item=item))
    assert [(e.type, e.content) for e in events] == [("status", "Bash: ls")]


def test_command_execution_completed_nonzero_emits_status() -> None:
    item = {
        "id": "i",
        "type": "commandExecution",
        "command": "false",
        "status": "completed",
        "exitCode": 1,
    }
    events = parse_codex_notification(_notif("item/completed", threadId="t", item=item))
    assert [(e.type, e.content) for e in events] == [("status", "Bash: false (exit 1)")]


def test_image_generation_completed_emits_image_message() -> None:
    item = {
        "id": "i",
        "type": "imageGeneration",
        "status": "completed",
        "savedPath": "/tmp/cat.png",
        "revisedPrompt": "a cat coding",
    }
    events = parse_codex_notification(_notif("item/completed", threadId="t", item=item))
    assert len(events) == 1
    e = events[0]
    assert e.type == "image_message"
    assert e.content == "/tmp/cat.png"
    assert e.session_id == "a cat coding"


def test_turn_completed_emits_result_sentinel() -> None:
    events = parse_codex_notification(
        _notif("turn/completed", threadId="t", turnId="t1", status="completed")
    )
    assert [(e.type, e.content) for e in events] == [("result", "")]


def test_unknown_method_returns_empty() -> None:
    events = parse_codex_notification(_notif("totally/made/up"))
    assert events == []


def test_error_with_will_retry_is_suppressed() -> None:
    # Codex emits intermediate errors with willRetry=true while it retries
    # internally (e.g. image-gen rate limit + retry). Surfacing each as a
    # user-facing message is just noise.
    events = parse_codex_notification(
        _notif(
            "error",
            threadId="t",
            turnId="x",
            willRetry=True,
            error={"message": "rate limited"},
        )
    )
    assert events == []


def test_error_terminal_surfaces_real_message() -> None:
    # Real ErrorNotification puts the message at params.error.message
    # (not params.message). Earlier code defaulted to "Codex error" and the
    # user saw "Codex error: Codex error" — useless. Extract the real text.
    events = parse_codex_notification(
        _notif(
            "error",
            threadId="t",
            turnId="x",
            willRetry=False,
            error={"message": "Usage limit exceeded"},
        )
    )
    assert [(e.type, e.content) for e in events] == [
        ("result_message", "Codex error: Usage limit exceeded")
    ]


def test_error_legacy_top_level_message() -> None:
    # If a server build still puts the message at the top level, accept it.
    events = parse_codex_notification(_notif("error", willRetry=False, message="something broke"))
    assert [(e.type, e.content) for e in events] == [
        ("result_message", "Codex error: something broke")
    ]

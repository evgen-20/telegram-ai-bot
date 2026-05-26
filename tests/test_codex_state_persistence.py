"""StateStore round-trips codex_sessions via load_codex_sessions helper."""

from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.services.tmux_state import (
    CodexSessionRecord,
    load_codex_sessions,
)


def test_round_trip(tmp_path: Path) -> None:
    sp = tmp_path / "state.json"
    sp.write_text(
        json.dumps(
            {
                "codex_sessions": {
                    "-1001:42": {"thread_id": "th-1", "cwd": "/home/x"},
                }
            }
        )
    )
    records = load_codex_sessions(sp)
    assert records == {
        (-1001, 42): CodexSessionRecord(thread_id="th-1", cwd="/home/x"),
    }


def test_thread_none_round_trip(tmp_path: Path) -> None:
    """Non-forum chats persist with the literal string 'None' as thread id."""
    sp = tmp_path / "state.json"
    sp.write_text(
        json.dumps(
            {
                "codex_sessions": {
                    "-1002:None": {"thread_id": "th-2", "cwd": "/srv"},
                }
            }
        )
    )
    records = load_codex_sessions(sp)
    assert records == {
        (-1002, None): CodexSessionRecord(thread_id="th-2", cwd="/srv"),
    }


def test_missing_key_returns_empty(tmp_path: Path) -> None:
    sp = tmp_path / "empty.json"
    sp.write_text("{}")
    assert load_codex_sessions(sp) == {}


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    sp = tmp_path / "nope.json"
    assert load_codex_sessions(sp) == {}


def test_malformed_json_returns_empty(tmp_path: Path) -> None:
    sp = tmp_path / "broken.json"
    sp.write_text("{not json")
    assert load_codex_sessions(sp) == {}


def test_malformed_entries_skipped(tmp_path: Path) -> None:
    sp = tmp_path / "state.json"
    sp.write_text(
        json.dumps(
            {
                "codex_sessions": {
                    "-1:5": {"thread_id": "ok", "cwd": "/a"},
                    "-1:6": {"thread_id": "missing-cwd"},  # missing field
                    "not-numeric": {"thread_id": "x", "cwd": "/b"},  # bad key shape
                }
            }
        )
    )
    records = load_codex_sessions(sp)
    # Only the valid one survives.
    assert records == {(-1, 5): CodexSessionRecord(thread_id="ok", cwd="/a")}

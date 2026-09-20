"""StateStore round-trips codex_sessions via load_codex_sessions helper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, cast

from telegram_bot.core.services.tmux_recovery import restore_all
from telegram_bot.core.services.tmux_state import (
    CodexSessionRecord,
    StateStore,
    TmuxSessionState,
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


def test_tmux_save_preserves_codex_sessions(tmp_path: Path) -> None:
    """A tmux state save must not wipe the codex_sessions key.

    Regression: StateStore.save() rebuilt the file from the tmux sessions
    dict alone, so every save clobbered the codex thread mapping written by
    CodexSessionManager._persist and codex topics lost their thread on the
    next bot restart.
    """
    sp = tmp_path / "state.json"
    sp.write_text(
        json.dumps(
            {
                "codex_sessions": {
                    "-1001:42": {"thread_id": "th-keep", "cwd": "/home/x"},
                }
            }
        )
    )

    store = StateStore(sp)
    store.save(
        {
            (-1001, 7): TmuxSessionState(
                session_name="cc-1001-7",
                session_dir=str(tmp_path / "sess"),
                session_id="sid-1",
                mode="free",
                cwd="/home/x",
                mcp_config="/tmp/mcp.json",
                chat_id=-1001,
            )
        }
    )

    raw = json.loads(sp.read_text())
    assert "-1001:7" in raw
    assert load_codex_sessions(sp) == {
        (-1001, 42): CodexSessionRecord(thread_id="th-keep", cwd="/home/x"),
    }


def test_tmux_save_without_codex_sessions_writes_no_key(tmp_path: Path) -> None:
    """No codex sessions on disk → no empty placeholder key is invented."""
    sp = tmp_path / "state.json"
    StateStore(sp).save(
        {
            (-1001, 7): TmuxSessionState(
                session_name="cc-1001-7",
                session_dir=str(tmp_path / "sess"),
                session_id="sid-1",
                mode="free",
                cwd="/home/x",
                mcp_config="/tmp/mcp.json",
                chat_id=-1001,
            )
        }
    )
    assert "codex_sessions" not in json.loads(sp.read_text())


def test_restore_all_skips_foreign_state_keys(tmp_path: Path, caplog: Any) -> None:
    """`codex_sessions` is not a channel key — recovery must step over it.

    `StateStore.save()` was taught to preserve the key, but `restore_all`
    still split every top-level key on ":" and blew up on this one with
    "not enough values to unpack", logging a traceback on every boot.
    """
    sp = tmp_path / "state.json"
    sp.write_text(
        json.dumps(
            {
                "codex_sessions": {"-1001:42": {"thread_id": "th-1", "cwd": "/home/x"}},
                "-1001:7": {
                    "session_name": "cc-1001-7",
                    "session_dir": str(tmp_path / "sess"),
                    "session_id": "sid-1",
                    "mode": "free",
                    "cwd": "/home/x",
                    "mcp_config": "/tmp/mcp.json",
                    "chat_id": -1001,
                    "runner_version": "claude-tui-v1",
                    "provider": "claude",
                },
            }
        )
    )

    class _FakeManager:
        def __init__(self) -> None:
            self._state_store = StateStore(sp)
            self._sessions: dict[Any, Any] = {}

        def _tmux_alive(self, name: str) -> bool:
            return True

        def _save_state(self) -> None:
            return None

    manager = _FakeManager()
    with caplog.at_level(logging.WARNING):
        restored = restore_all(cast(Any, manager), session_manager=None)

    assert list(restored) == [(-1001, 7)]
    assert "codex_sessions" not in caplog.text

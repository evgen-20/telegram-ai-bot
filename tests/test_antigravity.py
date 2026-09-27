from __future__ import annotations

import os
from pathlib import Path

import pytest

from telegram_bot.core.services import antigravity as agy


def _make_exec(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def test_configured_binary_is_used_when_safe(tmp_path: Path, monkeypatch) -> None:
    binary = _make_exec(tmp_path / "agy")
    monkeypatch.setenv("TELEGRAM_AGY_BIN", str(binary))

    assert agy.antigravity_binary() == str(binary)


def test_configured_binary_that_is_group_writable_is_rejected(tmp_path: Path, monkeypatch) -> None:
    binary = _make_exec(tmp_path / "agy", mode=0o775)
    monkeypatch.setenv("TELEGRAM_AGY_BIN", str(binary))

    with pytest.raises(RuntimeError):
        agy.antigravity_binary()
    assert agy.safe_antigravity_binary() is None


def test_standalone_install_path_is_preferred(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_AGY_BIN", raising=False)
    monkeypatch.setattr(agy.Path, "home", classmethod(lambda cls: tmp_path))
    binary = _make_exec(tmp_path / ".local" / "bin" / "agy")
    monkeypatch.setenv("PATH", "/nonexistent")

    assert agy.safe_antigravity_binary() == str(binary)


def test_missing_binary_is_reported_as_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_AGY_BIN", raising=False)
    monkeypatch.setattr(agy.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")

    assert agy.safe_antigravity_binary() is None


def test_process_env_carries_routing_and_pins_the_cli_version() -> None:
    env = agy.antigravity_process_env(
        (-1001, 42),
        binary="/opt/agy/bin/agy",
        base_env={"PATH": "/usr/bin", "HOME": "/home/u", "TELEGRAM_BOT_TOKEN": "secret"},
    )

    assert env["TELEGRAM_CHAT_ID"] == "-1001"
    assert env["TELEGRAM_THREAD_ID"] == "42"
    assert env["TELEGRAM_CONTEXT_LOCK"] == "1"
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == "/opt/agy/bin"
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_process_env_uses_empty_thread_for_private_chats() -> None:
    env = agy.antigravity_process_env((5, None), base_env={"PATH": "/usr/bin"})

    assert env["TELEGRAM_THREAD_ID"] == ""


def test_process_env_without_channel_has_no_routing() -> None:
    env = agy.antigravity_process_env(None, base_env={"PATH": "/usr/bin"})

    assert "TELEGRAM_CHAT_ID" not in env
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"


def test_exec_argv_for_a_new_conversation() -> None:
    argv = agy.build_exec_argv("hello", conversation_id=None, model=None, binary="/b/agy")

    assert argv == [
        "/b/agy",
        "-p",
        "hello",
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
    ]


def test_exec_argv_resumes_and_selects_model() -> None:
    cid = "75f258c1-2d19-4c99-8203-b941550c2bda"
    argv = agy.build_exec_argv(
        "hi", conversation_id=cid, model="gemini-3.1-pro-high", binary="/b/agy"
    )

    assert argv[argv.index("--conversation") + 1] == cid
    assert argv[argv.index("--model") + 1] == "gemini-3.1-pro-high"


def test_exec_argv_keeps_a_dash_prompt_as_the_print_value() -> None:
    argv = agy.build_exec_argv("--help me", conversation_id=None, model=None, binary="/b/agy")

    assert argv[argv.index("-p") + 1] == "--help me"


def test_tui_argv_for_new_and_resumed_conversations() -> None:
    cid = "13e3821c-34be-4324-8d5e-e10bedf6f16b"

    assert agy.build_tui_argv(conversation_id=None, model=None, binary="/b/agy") == [
        "/b/agy",
        "--dangerously-skip-permissions",
    ]
    assert agy.build_tui_argv(conversation_id=cid, model="m1", binary="/b/agy") == [
        "/b/agy",
        "--dangerously-skip-permissions",
        "--conversation",
        cid,
        "--model",
        "m1",
    ]


def test_tui_command_is_a_self_contained_env_invocation(monkeypatch) -> None:
    monkeypatch.setattr(agy, "antigravity_binary", lambda: "/b/agy")

    cmd = agy.build_tui_command((-100, 7), conversation_id=None, model=None)

    assert cmd[:2] == ["env", "-i"]
    assert "TELEGRAM_CHAT_ID=-100" in cmd
    assert "TELEGRAM_THREAD_ID=7" in cmd
    assert "AGY_CLI_DISABLE_AUTO_UPDATE=1" in cmd
    assert cmd[cmd.index("/b/agy") :] == ["/b/agy", "--dangerously-skip-permissions"]


def test_conversation_paths(tmp_path: Path) -> None:
    cid = "75f258c1-2d19-4c99-8203-b941550c2bda"

    assert agy.brain_dir(cid, home=tmp_path) == tmp_path / "brain" / cid
    assert agy.transcript_path(cid, home=tmp_path) == (
        tmp_path / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("75f258c1-2d19-4c99-8203-b941550c2bda", True),
        ("../etc", False),
        ("", False),
        ("75f258c1-2d19-4c99-8203-b941550c2bdaX", False),
    ],
)
def test_conversation_id_shape(value: str, expected: bool) -> None:
    assert agy.is_conversation_id(value) is expected

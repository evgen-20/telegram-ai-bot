from __future__ import annotations

import subprocess
from pathlib import Path

from telegram_bot.core.services.antigravity import ensure_bot_mcp_registered

EMPTY = "No MCP servers configured.\n"
WITH_BOT = (
    "NAME    TYPE   STATUS    COMMAND/URL\n"
    "fs      stdio  enabled   npx\n"
    "bot     stdio  disabled  bash\n"
)


class FakeRun:
    def __init__(self, list_output: str, *, add_returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._list_output = list_output
        self._add_returncode = add_returncode

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[1:3] == ["mcp", "list"]:
            return subprocess.CompletedProcess(argv, 0, self._list_output, "")
        return subprocess.CompletedProcess(argv, self._add_returncode, "", "boom")


def test_missing_bot_server_is_added(tmp_path: Path) -> None:
    run = FakeRun(EMPTY)

    assert ensure_bot_mcp_registered(tmp_path, binary="/b/agy", run=run)

    assert run.calls[-1] == [
        "/b/agy",
        "mcp",
        "add",
        "-e",
        f"APP_ROOT={tmp_path}",
        "-e",
        f"ENV_FILE={tmp_path / '.env'}",
        "-e",
        f"PROJECT_DIR={tmp_path}",
        "bot",
        "bash",
        str(tmp_path / "mcp-servers" / "bot" / "start.sh"),
    ]


def test_existing_bot_server_is_left_alone(tmp_path: Path) -> None:
    run = FakeRun(WITH_BOT)

    assert ensure_bot_mcp_registered(tmp_path, binary="/b/agy", run=run)

    assert [call[1:3] for call in run.calls] == [["mcp", "list"]]


def test_a_server_whose_name_only_starts_with_bot_does_not_count(tmp_path: Path) -> None:
    run = FakeRun("NAME  TYPE  STATUS  COMMAND/URL\nbotany  stdio  enabled  x\n")

    ensure_bot_mcp_registered(tmp_path, binary="/b/agy", run=run)

    assert run.calls[-1][1:3] == ["mcp", "add"]


def test_failures_are_reported_not_raised(tmp_path: Path) -> None:
    assert not ensure_bot_mcp_registered(
        tmp_path, binary="/b/agy", run=FakeRun(EMPTY, add_returncode=1)
    )

    def exploding(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 30)

    assert not ensure_bot_mcp_registered(tmp_path, binary="/b/agy", run=exploding)


def test_registration_runs_with_the_sanitized_agent_environment(tmp_path: Path) -> None:
    seen: list[dict[str, str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        seen.append(env)
        return subprocess.CompletedProcess(argv, 0, WITH_BOT, "")

    ensure_bot_mcp_registered(tmp_path, binary="/b/agy", run=run)

    assert seen
    assert "TELEGRAM_BOT_TOKEN" not in seen[0]


def test_bot_start_registers_the_mcp_server_before_polling() -> None:
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")

    assert "ensure_bot_mcp_registered" in source
    assert source.index("ensure_bot_mcp_registered") < source.index("dp.start_polling")
    # Only when agy is installed; the call must not block the event loop.
    assert "safe_antigravity_binary()" in source
    assert "asyncio.to_thread(\n            antigravity.ensure_bot_mcp_registered" in source

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.core.keyboards import model_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services import antigravity as agy
from telegram_bot.core.services.bot_commands import build_bot_commands
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.services.topic_runtime import BotDefaults, resolve_topic_runtime_config

MODELS_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
    "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
)


@pytest.fixture(autouse=True)
def _fresh_model_cache() -> None:
    agy.clear_model_cache()


def _run_returning(stdout: str, returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    return run, calls


def test_list_models_parses_the_cli_table() -> None:
    run, _ = _run_returning(MODELS_OUTPUT)

    assert agy.list_models(binary="/b/agy", run=run) == [
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
        ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
    ]


def test_list_models_is_cached() -> None:
    run, calls = _run_returning(MODELS_OUTPUT)

    agy.list_models(binary="/b/agy", run=run)
    agy.list_models(binary="/b/agy", run=run)

    assert len(calls) == 1


def test_list_models_failure_is_empty_and_not_cached() -> None:
    failing, _ = _run_returning("error: not logged in", returncode=1)
    assert agy.list_models(binary="/b/agy", run=failing) == []

    run, calls = _run_returning(MODELS_OUTPUT)
    assert agy.list_models(binary="/b/agy", run=run)
    assert len(calls) == 1


def _topics(path: Path, **topic: object) -> None:
    base = {"name": "t", "type": "project", "mode": "free", "engine": "antigravity"}
    path.write_text(json.dumps({"topics": {"10": {**base, **topic}}}))


async def test_model_override_merges_into_the_per_engine_map(tmp_path: Path) -> None:
    path = tmp_path / "topic_config.json"
    _topics(path, models={"claude": "opus"})
    config = TopicConfig(str(path), str(tmp_path))

    assert await config.update_model_override(10, "antigravity", "gemini-3.1-pro-high")

    topic = TopicConfig(str(path), str(tmp_path)).get_topic(10)
    assert topic.models == {"claude": "opus", "antigravity": "gemini-3.1-pro-high"}
    runtime = resolve_topic_runtime_config(topic, BotDefaults(cwd=tmp_path, mcp_config=None))
    assert runtime.model == "gemini-3.1-pro-high"


async def test_model_override_none_restores_the_default(tmp_path: Path) -> None:
    path = tmp_path / "topic_config.json"
    _topics(path, models={"antigravity": "gemini-3.1-pro-high", "claude": "opus"})
    config = TopicConfig(str(path), str(tmp_path))

    assert await config.update_model_override(10, "antigravity", None)

    assert TopicConfig(str(path), str(tmp_path)).get_topic(10).models == {"claude": "opus"}


async def test_model_override_rejects_unsafe_values(tmp_path: Path) -> None:
    path = tmp_path / "topic_config.json"
    _topics(path)
    config = TopicConfig(str(path), str(tmp_path))

    assert not await config.update_model_override(10, "antigravity", "bad model; rm -rf")


def test_model_keyboard_marks_the_current_model_and_offers_the_default() -> None:
    markup = model_keyboard(
        [("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"), ("m2", "Model Two")],
        current="gemini-3.1-pro-high",
    )
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert [b.callback_data for b in buttons] == [
        "model:gemini-3.1-pro-high",
        "model:m2",
        "model:",
    ]
    assert buttons[0].text.startswith("✅")
    assert not buttons[2].text.startswith("✅")


def test_model_keyboard_marks_default_when_no_override() -> None:
    markup = model_keyboard([("m1", "One")], current=None)
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert buttons[-1].text.startswith("✅")


def test_command_menu_offers_model_and_names_antigravity() -> None:
    commands = {c.command: c.description for c in build_bot_commands("en")}

    assert "model" in commands
    assert "Antigravity" in commands["engine"]


# --- handlers -----------------------------------------------------------------


def _message(thread_id: int | None = 10) -> MagicMock:
    message = MagicMock()
    message.chat.id = -100
    message.message_thread_id = thread_id
    message.is_topic_message = thread_id is not None
    message.answer = AsyncMock()
    return message


async def test_model_command_outside_antigravity_explains(tmp_path: Path) -> None:
    from telegram_bot.core.handlers.commands import handle_model_command

    path = tmp_path / "topic_config.json"
    _topics(path, engine="claude")
    message = _message()

    await handle_model_command(message, TopicConfig(str(path), str(tmp_path)))

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == t("ui.model_only_antigravity")


async def test_model_command_shows_the_agy_models(tmp_path: Path, monkeypatch) -> None:
    from telegram_bot.core.handlers.commands import handle_model_command

    path = tmp_path / "topic_config.json"
    _topics(path)
    monkeypatch.setattr(agy, "list_models", lambda **kwargs: [("m1", "Model One")])
    message = _message()

    await handle_model_command(message, TopicConfig(str(path), str(tmp_path)))

    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "model:m1"


def _callback(data: str, thread_id: int = 10) -> MagicMock:
    from aiogram.types import CallbackQuery

    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.message = _message(thread_id)
    callback.message.message_thread_id = thread_id
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    callback.from_user = None
    return callback


async def test_picking_a_model_persists_it_and_recycles_the_live_session(
    tmp_path: Path, monkeypatch
) -> None:
    from telegram_bot.core.handlers.commands import on_model_click

    path = tmp_path / "topic_config.json"
    _topics(path)
    config = TopicConfig(str(path), str(tmp_path))
    monkeypatch.setattr(agy, "list_models", lambda **kwargs: [("m1", "Model One")])
    tmux = MagicMock()
    tmux.is_active.return_value = True
    tmux.is_processing.return_value = False
    tmux.set_model = MagicMock()
    tmux.recycle = AsyncMock(return_value=True)
    queue = MagicMock()
    queue.is_busy.return_value = False

    await on_model_click(_callback("model:m1"), config, tmux, queue, MagicMock())

    assert TopicConfig(str(path), str(tmp_path)).get_topic(10).models == {"antigravity": "m1"}
    tmux.set_model.assert_called_once_with((-100, 10), "m1")
    tmux.recycle.assert_awaited_once()


async def test_an_unknown_model_is_refused(tmp_path: Path, monkeypatch) -> None:
    from telegram_bot.core.handlers.commands import on_model_click

    path = tmp_path / "topic_config.json"
    _topics(path)
    config = TopicConfig(str(path), str(tmp_path))
    monkeypatch.setattr(agy, "list_models", lambda **kwargs: [("m1", "Model One")])
    callback = _callback("model:not-listed")
    idle = MagicMock()
    idle.is_processing.return_value = False
    idle.is_busy.return_value = False

    await on_model_click(callback, config, idle, idle, MagicMock())

    callback.answer.assert_awaited_once_with(t("ui.model_invalid"), show_alert=True)
    assert TopicConfig(str(path), str(tmp_path)).get_topic(10).models == {}


async def test_model_change_is_refused_while_busy(tmp_path: Path, monkeypatch) -> None:
    from telegram_bot.core.handlers.commands import on_model_click

    path = tmp_path / "topic_config.json"
    _topics(path)
    config = TopicConfig(str(path), str(tmp_path))
    monkeypatch.setattr(agy, "list_models", lambda **kwargs: [("m1", "Model One")])
    tmux = MagicMock()
    tmux.is_processing.return_value = True
    callback = _callback("model:m1")

    await on_model_click(callback, config, tmux, MagicMock(), MagicMock())

    callback.answer.assert_awaited_once_with(t("ui.exec_mode_busy"), show_alert=True)
    assert TopicConfig(str(path), str(tmp_path)).get_topic(10).models == {}

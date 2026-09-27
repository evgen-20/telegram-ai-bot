from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from telegram_bot.core.keyboards import engine_keyboard
from telegram_bot.core.messages import t
from telegram_bot.core.services import providers
from telegram_bot.core.services.providers import (
    choose_available_engine,
    engine_display_name,
    is_engine_available,
)
from telegram_bot.core.services.session_backend import BackendDispatcher
from telegram_bot.core.services.topic_config import TopicConfig


def _write_topics(path: Path, engine: str) -> None:
    path.write_text(
        json.dumps(
            {
                "topics": {
                    "10": {
                        "name": "t",
                        "type": "project",
                        "mode": "free",
                        "cwd": None,
                        "mcp_config": None,
                        "stream_mode": "live",
                        "exec_mode": "tmux",
                        "engine": engine,
                    }
                }
            }
        )
    )


def test_topic_config_accepts_antigravity(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write_topics(config_path, "antigravity")

    config = TopicConfig(str(config_path), str(tmp_path))

    assert config.get_topic(10).engine == "antigravity"


async def test_topic_config_persists_an_antigravity_switch(tmp_path: Path) -> None:
    config_path = tmp_path / "topic_config.json"
    _write_topics(config_path, "claude")
    config = TopicConfig(str(config_path), str(tmp_path))

    assert await config.update_engine_model(10, "antigravity", "gemini-3.1-pro-high")

    saved = json.loads(config_path.read_text())["topics"]["10"]
    assert saved["engine"] == "antigravity"
    assert TopicConfig(str(config_path), str(tmp_path)).get_topic(10).engine == "antigravity"


def test_display_name() -> None:
    assert engine_display_name("antigravity") == "Antigravity"


def test_availability_follows_the_agy_binary(monkeypatch) -> None:
    monkeypatch.setattr(
        "telegram_bot.core.services.antigravity.safe_antigravity_binary", lambda: None
    )
    assert is_engine_available("antigravity") is False

    monkeypatch.setattr(
        "telegram_bot.core.services.antigravity.safe_antigravity_binary", lambda: "/b/agy"
    )
    assert is_engine_available("antigravity") is True


def test_missing_antigravity_never_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(providers, "is_engine_available", lambda engine: engine != "antigravity")

    assert choose_available_engine("antigravity") is None


def test_available_antigravity_is_chosen(monkeypatch) -> None:
    monkeypatch.setattr(providers, "is_engine_available", lambda engine: True)

    assert choose_available_engine("antigravity") == "antigravity"


def test_antigravity_is_not_a_fallback_for_other_engines(monkeypatch) -> None:
    monkeypatch.setattr(providers, "is_engine_available", lambda engine: engine == "antigravity")

    assert choose_available_engine("claude") is None
    assert choose_available_engine("codex") is None


def test_engine_keyboard_offers_antigravity_and_marks_current() -> None:
    markup = engine_keyboard("antigravity")
    buttons = [button for row in markup.inline_keyboard for button in row]

    assert [b.callback_data for b in buttons] == [
        "engine:claude",
        "engine:codex",
        "engine:antigravity",
    ]
    assert buttons[2].text.startswith("✅")
    assert not buttons[0].text.startswith("✅")


def test_dispatcher_routes_antigravity_to_its_backend() -> None:
    claude, codex, agy = MagicMock(), MagicMock(), MagicMock()
    dispatcher = BackendDispatcher(claude=claude, codex=codex, antigravity=agy)

    assert dispatcher.for_engine("antigravity") is agy


def test_dispatcher_without_antigravity_backend_uses_claude_backend() -> None:
    claude, codex = MagicMock(), MagicMock()
    dispatcher = BackendDispatcher(claude=claude, codex=codex)

    assert dispatcher.for_engine("antigravity") is claude


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_not_found_message_exists(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("BOT_LANG", lang)
    from telegram_bot.core import messages

    messages.reset_lang_cache()
    try:
        text = t("ui.antigravity_not_found")
    finally:
        messages.reset_lang_cache()

    assert "Antigravity" in text
    assert text != "ui.antigravity_not_found"


async def test_subprocess_topic_without_agy_gets_an_explicit_answer(
    tmp_path: Path, monkeypatch
) -> None:
    from telegram_bot.core.config import Settings
    from telegram_bot.core.services.claude import SessionManager

    config_path = tmp_path / "topic_config.json"
    _write_topics(config_path, "antigravity")
    topic_config = TopicConfig(str(config_path), str(tmp_path))
    settings = Settings(_env_file=None, telegram_bot_token="x", project_root=str(tmp_path))
    manager = SessionManager(settings, topic_config=topic_config)
    monkeypatch.setattr(
        "telegram_bot.core.services.claude.choose_available_engine", lambda engine: None
    )
    run = MagicMock()
    monkeypatch.setattr(manager, "_run_cc_stream", run)

    answer = await manager.send_stream((-100, 10), "hi", lambda _event: None)

    assert answer == t("ui.antigravity_not_found")
    run.assert_not_called()
    assert topic_config.get_topic(10).engine == "antigravity"

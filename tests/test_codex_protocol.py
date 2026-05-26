"""Codex JSON-RPC TypedDict shapes — sanity-check via construction."""

from __future__ import annotations

from telegram_bot.core.services.codex_protocol import (
    AgentMessageDeltaParams,
    CommandExecutionItem,
    ImageGenerationItem,
    InitializeParams,
    ItemCompletedParams,
    ItemStartedParams,
    ThreadStartParams,
    ThreadStartResponse,
    TurnCompletedParams,
    TurnStartParams,
)


def test_initialize_params_shape() -> None:
    p: InitializeParams = {
        "clientInfo": {"name": "telegram-ai-agent", "version": "0.1"},
        "capabilities": {},
    }
    assert p["clientInfo"]["name"] == "telegram-ai-agent"


def test_thread_start_params() -> None:
    p: ThreadStartParams = {"cwd": "/tmp", "model": None}
    assert p["cwd"] == "/tmp"


def test_thread_start_response() -> None:
    r: ThreadStartResponse = {"threadId": "abc-123"}
    assert r["threadId"] == "abc-123"


def test_turn_start_params() -> None:
    p: TurnStartParams = {"threadId": "abc-123", "prompt": "hello"}
    assert p["threadId"] == "abc-123"


def test_agent_message_delta() -> None:
    p: AgentMessageDeltaParams = {
        "threadId": "abc",
        "itemId": "item-1",
        "delta": "hello",
    }
    assert p["delta"] == "hello"


def test_item_started_command_execution() -> None:
    item: CommandExecutionItem = {
        "id": "i1",
        "type": "commandExecution",
        "command": "ls -la",
        "status": "in_progress",
    }
    p: ItemStartedParams = {"threadId": "abc", "item": item}
    assert p["item"]["type"] == "commandExecution"


def test_item_completed_image_generation() -> None:
    item: ImageGenerationItem = {
        "id": "i2",
        "type": "imageGeneration",
        "status": "completed",
        "savedPath": "/tmp/img.png",
        "revisedPrompt": "a cat",
    }
    p: ItemCompletedParams = {"threadId": "abc", "item": item}
    assert p["item"]["type"] == "imageGeneration"


def test_turn_completed() -> None:
    p: TurnCompletedParams = {
        "threadId": "abc",
        "turnId": "t1",
        "status": "completed",
    }
    assert p["status"] == "completed"

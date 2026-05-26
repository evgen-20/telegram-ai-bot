"""Pure mapping from Codex JSON-RPC notifications to StreamEvent lists.

Keeping this as a pure function makes it trivially testable and lets the
session manager remain a thin orchestrator.

Behaviour matches the table in
`docs/superpowers/specs/2026-05-26-codex-app-server-adapter-design.md`
section 6 ("Event mapping").
"""

from __future__ import annotations

import logging
from typing import Any

from telegram_bot.core.services.cc_events import StreamEvent

logger = logging.getLogger("codex_events")

_WARNED_METHODS: set[str] = set()


def parse_codex_notification(notif: dict[str, Any]) -> list[StreamEvent]:
    method = notif.get("method")
    params = notif.get("params") or {}
    if not isinstance(method, str) or not isinstance(params, dict):
        return []

    if method == "item/agentMessage/delta":
        delta = params.get("delta")
        if isinstance(delta, str) and delta:
            return [StreamEvent("text", delta)]
        return []

    if method == "item/completed":
        return _parse_item_completed(params)

    if method == "item/started":
        return _parse_item_started(params)

    if method == "turn/completed":
        return [StreamEvent("result", "")]

    if method == "error":
        msg = str(params.get("message") or "Codex error")
        return [StreamEvent("result_message", f"Codex error: {msg}")]

    if method not in _WARNED_METHODS:
        _WARNED_METHODS.add(method)
        logger.warning("unhandled codex notification method=%s (will not warn again)", method)
    logger.debug("codex notification method=%s params=%s", method, params)
    return []


def _parse_item_started(params: dict[str, Any]) -> list[StreamEvent]:
    item = params.get("item") or {}
    if not isinstance(item, dict):
        return []
    kind = item.get("type")
    if kind == "commandExecution":
        cmd = str(item.get("command") or "")
        return [StreamEvent("status", f"Bash: {cmd}".rstrip())]
    if kind == "fileChange":
        path = str(item.get("path") or "")
        return [StreamEvent("status", f"Edit: {path}".rstrip())]
    if kind == "mcpToolCall":
        tool = str(item.get("toolName") or item.get("serverName") or "tool")
        return [StreamEvent("status", f"MCP: {tool}")]
    if kind == "imageGeneration":
        return [StreamEvent("status", "Generating image…")]
    return []


def _parse_item_completed(params: dict[str, Any]) -> list[StreamEvent]:
    item = params.get("item") or {}
    if not isinstance(item, dict):
        return []
    kind = item.get("type")

    if kind == "agentMessage":
        phase = item.get("phase")
        text = item.get("text")
        if phase == "final_answer" and isinstance(text, str) and text:
            return [StreamEvent("result_message", text)]
        return []

    if kind == "commandExecution":
        exit_code = item.get("exitCode")
        cmd = str(item.get("command") or "")
        if isinstance(exit_code, int) and exit_code != 0:
            return [StreamEvent("status", f"Bash: {cmd} (exit {exit_code})")]
        return []

    if kind == "fileChange":
        if item.get("status") == "error":
            err = str(item.get("error") or "unknown")
            path = str(item.get("path") or "")
            return [StreamEvent("status", f"Edit: {path} failed: {err}")]
        return []

    if kind == "mcpToolCall":
        if item.get("status") == "error":
            err = str(item.get("error") or "unknown")
            tool = str(item.get("toolName") or "tool")
            return [StreamEvent("status", f"MCP: {tool} failed: {err}")]
        return []

    if kind == "imageGeneration":
        if item.get("status") == "completed":
            saved = item.get("savedPath")
            caption = item.get("revisedPrompt")
            if isinstance(saved, str) and saved:
                return [
                    StreamEvent(
                        "image_message",
                        saved,
                        caption if isinstance(caption, str) else None,
                    )
                ]
            return []
        err = str(item.get("error") or "unknown")
        return [StreamEvent("status", f"Image generation failed: {err}")]

    return []

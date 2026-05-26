"""TypedDict shapes for the Codex app-server JSON-RPC protocol.

Mirrors a hand-picked subset of `docs/codex-protocol/schemas/`. Update
in lockstep with that reference when bumping the Codex version.

Codex >= 0.133.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ---- initialize ---------------------------------------------------------


class ClientInfo(TypedDict):
    name: str
    version: str


class InitializeParams(TypedDict):
    clientInfo: ClientInfo
    capabilities: dict[str, Any]


class InitializeResult(TypedDict, total=False):
    serverInfo: dict[str, str]
    capabilities: dict[str, Any]


# ---- thread / turn ------------------------------------------------------


class ThreadStartParams(TypedDict, total=False):
    cwd: str
    model: str | None


class ThreadStartResponse(TypedDict):
    threadId: str


class ThreadResumeParams(TypedDict):
    threadId: str


class TurnStartParams(TypedDict):
    threadId: str
    prompt: str


class TurnInterruptParams(TypedDict):
    threadId: str
    turnId: str


class TurnCompletedParams(TypedDict, total=False):
    threadId: str
    turnId: str
    status: str  # "completed" | "error" | "cancelled"
    error: str


# ---- item lifecycle -----------------------------------------------------


class AgentMessageItem(TypedDict, total=False):
    id: str
    type: Literal["agentMessage"]
    text: str
    status: str
    phase: str  # "final_answer" on completed


class CommandExecutionItem(TypedDict, total=False):
    id: str
    type: Literal["commandExecution"]
    command: str
    status: str
    exitCode: int


class FileChangeItem(TypedDict, total=False):
    id: str
    type: Literal["fileChange"]
    path: str
    status: str
    error: str


class McpToolCallItem(TypedDict, total=False):
    id: str
    type: Literal["mcpToolCall"]
    serverName: str
    toolName: str
    status: str
    error: str


class ImageGenerationItem(TypedDict, total=False):
    id: str
    type: Literal["imageGeneration"]
    status: str
    savedPath: str | None
    revisedPrompt: str | None


# Discriminated by `type` field in JSON.
ThreadItem = (
    AgentMessageItem | CommandExecutionItem | FileChangeItem | McpToolCallItem | ImageGenerationItem
)


class ItemStartedParams(TypedDict):
    threadId: str
    item: ThreadItem


class ItemCompletedParams(TypedDict):
    threadId: str
    item: ThreadItem


class AgentMessageDeltaParams(TypedDict):
    threadId: str
    itemId: str
    delta: str


class CommandExecutionOutputDeltaParams(TypedDict, total=False):
    threadId: str
    itemId: str
    stream: str  # "stdout" | "stderr"
    chunk: str


# ---- approval RPCs (server -> client) -----------------------------------


class ApprovalParams(TypedDict, total=False):
    threadId: str
    requestId: str
    summary: str


class ApprovalResponse(TypedDict):
    decision: Literal["approve", "deny"]


# ---- generic JSON-RPC envelopes -----------------------------------------


class JsonRpcRequest(TypedDict, total=False):
    jsonrpc: Literal["2.0"]
    id: int | str
    method: str
    params: dict[str, Any]


class JsonRpcResponse(TypedDict, total=False):
    jsonrpc: Literal["2.0"]
    id: int | str
    result: dict[str, Any]
    error: dict[str, Any]


class JsonRpcNotification(TypedDict, total=False):
    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any]

# Codex app-server Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken Codex JSONL-rollout transcript path with a full-parity adapter built on `codex app-server proxy` JSON-RPC, so all Telegram bot features (streaming text, command status, image generation, restart recovery, cancel, switch, clear) work for `engine=codex` topics on Codex ≥ 0.133.

**Architecture:** Introduce a `SessionBackend` Protocol that both the existing tmux-based path (claude) and the new Codex path implement. The codex backend spawns a `codex app-server proxy` subprocess per session and pipes JSON-RPC over its stdio. Handlers stop calling `tmux_manager` directly — they call a dispatcher that picks the right backend by topic engine. Legacy file-rollout code is deleted, not gated.

**Tech Stack:** Python 3.12, asyncio, stdlib `asyncio.subprocess` + `json` (no new deps); pytest + mypy strict + ruff (CI); Codex CLI ≥ 0.133 (`@openai/codex` npm).

**Spec:** `docs/superpowers/specs/2026-05-26-codex-app-server-adapter-design.md`

**Branch:** `feat/codex-app-server-adapter` (off `origin/main` from `pavel-molyanov/telegram-ai-agent`). Push target: fork `evgen-20/telegram-ai-bot`. Upstream PR after manual validation.

---

## File Map

### New files

| Path | Responsibility |
|------|----------------|
| `src/telegram_bot/core/services/session_backend.py` | `SessionBackend` Protocol mirroring the slice of `tmux_manager` that handlers depend on; `BackendDispatcher` that selects backend by topic engine. |
| `src/telegram_bot/core/services/codex_protocol.py` | `TypedDict`s for JSON-RPC messages (`InitializeParams`, `ThreadStartParams`, `TurnStartParams`, `ServerNotification` shapes, `ImageGenerationThreadItem`, etc.). |
| `src/telegram_bot/core/services/codex_daemon.py` | Singleton daemon ensure-running + healthcheck for the `codex app-server` process. |
| `src/telegram_bot/core/services/codex_app_server.py` | `CodexAppServerClient`: per-session JSON-RPC client over `codex app-server proxy` subprocess. |
| `src/telegram_bot/core/services/codex_events.py` | `parse_codex_notification(notif) -> list[StreamEvent]` — pure mapping function. |
| `src/telegram_bot/core/services/codex_session_manager.py` | `CodexSessionManager` — implements `SessionBackend`. Owns one `CodexAppServerClient` per channel, drives `thread/start`/`turn/start`, dispatches notifications, persists state. |
| `tests/test_session_backend.py` | Protocol + dispatcher tests. |
| `tests/test_codex_protocol.py` | TypedDict round-trip + JSON Schema reference equality. |
| `tests/test_codex_daemon.py` | Daemon ensure/healthcheck/restart with subprocess mocks. |
| `tests/test_codex_app_server.py` | JSON-RPC client tests using bash-stub subprocess emitting fixture frames. |
| `tests/test_codex_events.py` | Event mapping table coverage. |
| `tests/test_codex_session_manager.py` | High-level orchestrator tests against scripted notification sequences. |
| `tests/test_codex_state_persistence.py` | Codex section round-trip in `StateStore`. |
| `tests/fixtures/codex_app_server/*.json` | Recorded JSON-RPC notification sequences for fixture-based tests. |
| `scripts/smoke-codex.sh` | Manual smoke driver against real Codex. |
| `docs/codex-protocol/README.md` | Notes + dumped JSON Schema reference. |

### Modified files

| Path | Change |
|------|--------|
| `src/telegram_bot/core/services/cc_events.py` | Add `"image_message"` to `StreamEvent.type` Literal. |
| `src/telegram_bot/core/services/providers.py` | Strip `CodexAdapter` of `build_tui_*`, `parse_tui_event`, `find_tui_transcript`, `locate_tui_transcript`, `transcript_path_for_state`. Keep `binary()` and metadata used by topic-setup UI. |
| `src/telegram_bot/core/services/tmux_manager.py` | Remove `_locate_codex_transcript_after_send`, `_codex_start_snapshots`, all `state.provider == "codex"` branches. Become claude-only. |
| `src/telegram_bot/core/services/tmux_state.py` | Extend `StateStore` to read/write a new `codex_sessions` top-level dict; add `CodexSessionState` dataclass. |
| `src/telegram_bot/core/services/tmux_recovery.py` | Strip codex branches; codex recovery is owned by `CodexSessionManager.restore_all`. |
| `src/telegram_bot/core/handlers/streaming.py` | Replace direct `tmux_manager` calls with `dispatcher.for_channel(key)` (returns `SessionBackend`). Handle new `image_message` StreamEvent — send photo via Bot API. |
| `src/telegram_bot/core/handlers/_dispatch.py` | Same dispatcher swap. |
| `src/telegram_bot/core/handlers/text.py` | Same dispatcher swap. |
| `src/telegram_bot/core/handlers/cancel.py` | Same. |
| `src/telegram_bot/core/handlers/commands.py` | `/new`, `/repo`, `/status` routed through dispatcher. |
| `src/telegram_bot/__main__.py` | Construct `CodexSessionManager`, register in `BackendDispatcher` alongside the existing claude `TmuxManager`. |
| `src/telegram_bot/core/services/forward_batcher.py` | If needed for image-message ordering — verify; touch only if test forces. |

---

## Phase 0: Pre-flight

### Task 0.1: Confirm clean working tree on feature branch

**Files:** none

- [ ] **Step 1: Verify branch and clean state**

Run:
```bash
git -C /home/evgen/projects/telegram-ai-agent rev-parse --abbrev-ref HEAD
git -C /home/evgen/projects/telegram-ai-agent status --short
```
Expected: branch `feat/codex-app-server-adapter`; working tree clean (the `__main__.py` patch was stashed during spec writing).

- [ ] **Step 2: Run baseline checks (must already be green on `origin/main`)**

Run:
```bash
cd /home/evgen/projects/telegram-ai-agent
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
```
Expected: all four commands exit 0. If anything fails on baseline, fix or rebase before proceeding.

### Task 0.2: Dump reference JSON Schema into repo

**Files:**
- Create: `docs/codex-protocol/README.md`
- Create: `docs/codex-protocol/schemas/` (directory with dumped JSON files)

- [ ] **Step 1: Dump schema**

Run:
```bash
mkdir -p docs/codex-protocol/schemas
codex app-server generate-json-schema --out docs/codex-protocol/schemas
codex --version > docs/codex-protocol/codex-version.txt
```

- [ ] **Step 2: Write reference README**

Create `docs/codex-protocol/README.md` with:
```markdown
# Codex app-server protocol reference

This directory holds a frozen dump of the JSON Schema for the
`codex app-server` JSON-RPC protocol. It is reference material; the
adapter's TypedDicts in `src/telegram_bot/core/services/codex_protocol.py`
must stay aligned with these files.

Refresh when bumping Codex:

```bash
codex app-server generate-json-schema --out docs/codex-protocol/schemas
codex --version > docs/codex-protocol/codex-version.txt
git diff docs/codex-protocol/
```

Diff against the committed copy to spot breaking changes before they hit
production. The adapter is targeted at Codex >= 0.133.
```

- [ ] **Step 3: Commit**

```bash
git add docs/codex-protocol/
git commit -m "docs: vendor codex app-server JSON Schema reference"
```

---

## Phase 1: SessionBackend Protocol

This phase introduces the seam handlers will call through. The protocol
covers only the slice that handlers actually need. Methods unique to one
backend (e.g. tmux modal watchdog) stay private to that backend.

### Task 1.1: Define SessionBackend Protocol (TDD red)

**Files:**
- Create: `src/telegram_bot/core/services/session_backend.py`
- Create: `tests/test_session_backend.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_backend.py`:
```python
"""Tests for the SessionBackend Protocol and BackendDispatcher."""
from __future__ import annotations

import pytest

from telegram_bot.core.services.session_backend import (
    BackendDispatcher,
    SessionBackend,
)


class _StubBackend:
    """Tiny in-memory backend satisfying the Protocol shape."""

    def __init__(self, name: str) -> None:
        self.name = name

    def is_active(self, channel_key: tuple[int, int | None]) -> bool:
        return False

    def is_processing(self, channel_key: tuple[int, int | None]) -> bool:
        return False


def test_protocol_accepts_stub() -> None:
    backend: SessionBackend = _StubBackend("test")  # type: ignore[assignment]
    assert backend.is_active((1, 2)) is False


def test_dispatcher_picks_backend_by_engine() -> None:
    claude = _StubBackend("claude")
    codex = _StubBackend("codex")
    dispatcher = BackendDispatcher(claude=claude, codex=codex)  # type: ignore[arg-type]

    assert dispatcher.for_engine("claude") is claude
    assert dispatcher.for_engine("codex") is codex


def test_dispatcher_unknown_engine_raises() -> None:
    claude = _StubBackend("claude")
    codex = _StubBackend("codex")
    dispatcher = BackendDispatcher(claude=claude, codex=codex)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unknown engine"):
        dispatcher.for_engine("gemini")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_backend.py -v`
Expected: FAIL with `ImportError: cannot import name ...`.

- [ ] **Step 3: Write minimal implementation**

Create `src/telegram_bot/core/services/session_backend.py`:
```python
"""Backend abstraction for Telegram bot session managers.

`SessionBackend` is the surface area used by Telegram handlers. Both the
tmux-based path (claude) and the codex app-server path implement it.
`BackendDispatcher` picks the right one for a topic's engine.

Methods unique to one backend (tmux modal watchdog; codex thread/resume)
stay private to that backend's concrete class.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Protocol, runtime_checkable

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.topic_config import Engine
from telegram_bot.core.types import ChannelKey


@runtime_checkable
class SessionBackend(Protocol):
    """The slice of session-management used by Telegram handlers.

    Each method matches an existing tmux_manager.TmuxManager method by name
    and signature so callers can be migrated one site at a time.
    """

    def is_active(self, channel_key: ChannelKey) -> bool: ...
    def is_processing(self, channel_key: ChannelKey) -> bool: ...
    def is_tailing(self, channel_key: ChannelKey) -> bool: ...

    def get_session_id(self, channel_key: ChannelKey) -> str | None: ...
    def get_session_name(self, channel_key: ChannelKey) -> str | None: ...
    def get_provider_model(
        self, channel_key: ChannelKey
    ) -> tuple[str | None, str | None]: ...

    async def start_session(
        self,
        channel_key: ChannelKey,
        *,
        cwd: str,
        resume: bool,
        session_id: str | None = None,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> None: ...

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str: ...

    async def send_direct(self, channel_key: ChannelKey, prompt: str) -> bool: ...

    async def close_buffer(self, channel_key: ChannelKey) -> None: ...
    async def cancel(self, channel_key: ChannelKey) -> None: ...
    async def kill(self, channel_key: ChannelKey) -> None: ...

    async def clear_context(
        self, channel_key: ChannelKey, session_manager: object
    ) -> bool: ...

    async def switch_session(
        self,
        channel_key: ChannelKey,
        *,
        cwd: str,
        session_id: str,
    ) -> None: ...

    async def switch_or_start_session(
        self,
        channel_key: ChannelKey,
        *,
        cwd: str,
        session_id: str | None,
    ) -> None: ...


class BackendDispatcher:
    """Returns the right backend for a given engine name."""

    def __init__(self, *, claude: SessionBackend, codex: SessionBackend) -> None:
        self._by_engine: dict[Engine, SessionBackend] = {
            "claude": claude,
            "codex": codex,
        }

    def for_engine(self, engine: Engine) -> SessionBackend:
        try:
            return self._by_engine[engine]
        except KeyError as exc:
            raise ValueError(f"unknown engine: {engine!r}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_backend.py -v`
Expected: 3 passed.

- [ ] **Step 5: Run mypy + ruff**

```bash
uv run mypy src/telegram_bot/core/services/session_backend.py tests/test_session_backend.py
uv run ruff check src/telegram_bot/core/services/session_backend.py tests/test_session_backend.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/session_backend.py tests/test_session_backend.py
git commit -m "feat: add SessionBackend Protocol and BackendDispatcher"
```

### Task 1.2: Make TmuxManager satisfy SessionBackend (no behaviour change)

**Files:**
- Modify: `src/telegram_bot/core/services/tmux_manager.py` (only add `: SessionBackend` annotation in test if useful — no code change yet, just type-checking)
- Create: `tests/test_tmux_manager_protocol.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_tmux_manager_protocol.py`:
```python
"""TmuxManager must satisfy SessionBackend."""
from __future__ import annotations

from telegram_bot.core.services.session_backend import SessionBackend
from telegram_bot.core.services.tmux_manager import TmuxManager


def test_tmux_manager_satisfies_protocol() -> None:
    # Runtime Protocol check — uses isinstance against the @runtime_checkable
    # Protocol. This won't catch type mismatches, only missing attributes;
    # mypy --strict in CI catches signature drift.
    assert hasattr(TmuxManager, "start_session")
    assert hasattr(TmuxManager, "send_stream")
    assert hasattr(TmuxManager, "close_buffer")
    assert hasattr(TmuxManager, "cancel")
    assert hasattr(TmuxManager, "is_active")
    assert hasattr(TmuxManager, "is_processing")
    # Protocol-level assignment compile-check is handled by mypy below.
    _check_assignable: type[SessionBackend] = TmuxManager  # type: ignore[assignment]
    del _check_assignable
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_tmux_manager_protocol.py -v && uv run mypy tests/test_tmux_manager_protocol.py`
Expected: pytest passes; mypy may flag the `# type: ignore[assignment]` if Protocol matches — that's fine. If mypy raises a *different* error, the protocol does not match — adjust the protocol signatures in `session_backend.py` to match `TmuxManager`'s actual signatures.

- [ ] **Step 3: Iterate signature alignment until mypy is clean**

For each mypy error, copy the exact signature from `tmux_manager.py` into the Protocol. Keep iterating until both mypy and pytest pass. **Do not change TmuxManager** — it is the source of truth.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tmux_manager_protocol.py src/telegram_bot/core/services/session_backend.py
git commit -m "test: verify TmuxManager satisfies SessionBackend Protocol"
```

---

## Phase 2: Codex protocol TypedDicts

The schemas in `docs/codex-protocol/schemas/` define ~80 message shapes. We
only need types for the methods/notifications the bot uses. Defining them
as `TypedDict`s gives mypy coverage of the parser.

### Task 2.1: Define request/response TypedDicts

**Files:**
- Create: `src/telegram_bot/core/services/codex_protocol.py`
- Create: `tests/test_codex_protocol.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_protocol.py`:
```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_protocol.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Write minimal implementation**

Create `src/telegram_bot/core/services/codex_protocol.py`:
```python
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
    status: str   # "completed" | "error" | "cancelled"
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
    AgentMessageItem
    | CommandExecutionItem
    | FileChangeItem
    | McpToolCallItem
    | ImageGenerationItem
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
```

- [ ] **Step 4: Run test + mypy**

```bash
uv run pytest tests/test_codex_protocol.py -v
uv run mypy src/telegram_bot/core/services/codex_protocol.py tests/test_codex_protocol.py
```
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/services/codex_protocol.py tests/test_codex_protocol.py
git commit -m "feat: add Codex app-server JSON-RPC TypedDicts"
```

---

## Phase 3: CodexAppServerClient (JSON-RPC over subprocess)

Per-session subprocess wrapping `codex app-server proxy`. One read loop,
request/response futures, notification dispatch, auto-approve handler,
reconnect-on-EOF.

### Task 3.1: Establish JSON-line read loop

**Files:**
- Create: `src/telegram_bot/core/services/codex_app_server.py`
- Create: `tests/test_codex_app_server.py`
- Create: `tests/fixtures/codex_app_server/initialize_then_thread_start.jsonl`

- [ ] **Step 1: Create fixture frame file**

Create `tests/fixtures/codex_app_server/initialize_then_thread_start.jsonl`:
```jsonl
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"codex","version":"0.133.0"},"capabilities":{}}}
{"jsonrpc":"2.0","id":2,"result":{"threadId":"thread-deadbeef"}}
{"jsonrpc":"2.0","method":"turn/started","params":{"threadId":"thread-deadbeef","turnId":"turn-1"}}
{"jsonrpc":"2.0","method":"item/agentMessage/delta","params":{"threadId":"thread-deadbeef","itemId":"item-1","delta":"Hello"}}
{"jsonrpc":"2.0","method":"item/completed","params":{"threadId":"thread-deadbeef","item":{"id":"item-1","type":"agentMessage","text":"Hello","phase":"final_answer","status":"completed"}}}
{"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":"thread-deadbeef","turnId":"turn-1","status":"completed"}}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_codex_app_server.py`:
```python
"""Tests for CodexAppServerClient using a bash subprocess that prints fixture frames."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from telegram_bot.core.services.codex_app_server import CodexAppServerClient

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "codex_app_server"


def _stub_command(fixture_name: str) -> list[str]:
    """Bash that cats a fixture file slowly (one line at a time) and exits."""
    path = FIXTURE_DIR / fixture_name
    # `cat` with newline-buffered output is enough; the client reads line-by-line.
    return ["bash", "-c", f"cat {path!s}; sleep 0.05"]


@pytest.mark.asyncio
async def test_initialize_and_thread_start_round_trip() -> None:
    notifications: list[dict] = []

    async def on_notification(notif: dict) -> None:
        notifications.append(notif)

    client = CodexAppServerClient(
        command=_stub_command("initialize_then_thread_start.jsonl"),
        on_notification=on_notification,
    )
    async with client:
        init_result = await client.initialize(
            client_name="test", client_version="0.0"
        )
        assert init_result["serverInfo"]["name"] == "codex"

        ts_result = await client.thread_start(cwd="/tmp", model=None)
        assert ts_result["threadId"] == "thread-deadbeef"

    # Drain notifications produced after the responses.
    await asyncio.sleep(0.2)
    methods = [n["method"] for n in notifications]
    assert "turn/started" in methods
    assert "item/agentMessage/delta" in methods
    assert "item/completed" in methods
    assert "turn/completed" in methods
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_app_server.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Write minimal implementation**

Create `src/telegram_bot/core/services/codex_app_server.py`:
```python
"""Async JSON-RPC client over `codex app-server proxy` (or any subprocess
that speaks the same JSON-line protocol on stdio).

Single read loop per client. Request/response correlation via `id` field.
Notifications are pushed through `on_notification(notif: dict)`. Approval
RPCs from server are auto-approved (the bot uses
`--dangerously-bypass-approvals-and-sandbox` semantics).

Reconnect on subprocess EOF: terminate, respawn, re-initialize. Caller is
responsible for re-issuing `thread/resume` after reconnect (handled by
`CodexSessionManager`).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from telegram_bot.core.services.codex_protocol import (
    ApprovalResponse,
    InitializeResult,
    ThreadStartResponse,
)

logger = logging.getLogger("codex_app_server")

_APPROVAL_METHODS: frozenset[str] = frozenset({
    "execCommandApproval",
    "applyPatchApproval",
    "commandExecutionRequestApproval",
    "fileChangeRequestApproval",
})


class CodexAppServerClient:
    def __init__(
        self,
        *,
        command: list[str],
        on_notification: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> None:
        self._command = command
        self._on_notification = on_notification
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._closed = False

    async def __aenter__(self) -> "CodexAppServerClient":
        await self._spawn()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- public API -----------------------------------------------------

    async def initialize(
        self, *, client_name: str, client_version: str
    ) -> InitializeResult:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {"name": client_name, "version": client_version},
                "capabilities": {},
            },
        )
        return result  # type: ignore[return-value]

    async def thread_start(
        self, *, cwd: str, model: str | None
    ) -> ThreadStartResponse:
        params: dict[str, Any] = {"cwd": cwd}
        if model is not None:
            params["model"] = model
        result = await self._request("thread/start", params)
        return result  # type: ignore[return-value]

    async def thread_resume(self, *, thread_id: str) -> None:
        await self._request("thread/resume", {"threadId": thread_id})

    async def turn_start(self, *, thread_id: str, prompt: str) -> None:
        # Fire-and-forget: response is delivered via notifications.
        await self._request(
            "turn/start", {"threadId": thread_id, "prompt": prompt}
        )

    async def turn_interrupt(self, *, thread_id: str, turn_id: str) -> None:
        await self._request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    async def close(self) -> None:
        self._closed = True
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()

    # ---- internals ------------------------------------------------------

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(
            self._read_loop(), name="codex_read_loop"
        )

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while not self._closed:
            line = await self._proc.stdout.readline()
            if not line:
                logger.info("codex app-server stdout closed")
                self._fail_pending_with_eof()
                return
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("non-JSON line from codex: %s", line[:200])
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        # Response: has `id` and either `result` or `error`.
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(
                        CodexRpcError(msg["error"].get("message", "unknown"))
                    )
                else:
                    fut.set_result(msg.get("result", {}))
            return

        # Server-initiated request (e.g. approval). Has `id` and `method`.
        if "id" in msg and "method" in msg:
            await self._handle_server_request(msg)
            return

        # Notification: has `method` and no `id`.
        if "method" in msg:
            try:
                ret = self._on_notification(msg)
                if asyncio.iscoroutine(ret):
                    await ret
            except Exception:
                logger.exception("on_notification handler raised")
            return

        logger.warning("unrecognised JSON-RPC frame: %s", msg)

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = msg["method"]
        request_id = msg["id"]
        if method in _APPROVAL_METHODS:
            response: ApprovalResponse = {"decision": "approve"}
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "result": response}
            )
            return
        # Unknown server request: respond with error.
        logger.warning("unknown server request method: %s", method)
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not implemented"},
            }
        )

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("client not started")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("client not started")
        line = (json.dumps(msg) + "\n").encode("utf-8")
        self._proc.stdin.write(line)
        await self._proc.stdin.drain()

    def _fail_pending_with_eof(self) -> None:
        exc = CodexEof("codex app-server stdout closed")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class CodexRpcError(RuntimeError):
    """JSON-RPC error response from the server."""


class CodexEof(RuntimeError):
    """The codex subprocess closed stdout."""
```

- [ ] **Step 5: Run test**

Run: `uv run pytest tests/test_codex_app_server.py -v`
Expected: PASS.

- [ ] **Step 6: Run lint/types**

```bash
uv run ruff check src/telegram_bot/core/services/codex_app_server.py tests/test_codex_app_server.py
uv run mypy src/telegram_bot/core/services/codex_app_server.py tests/test_codex_app_server.py
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/telegram_bot/core/services/codex_app_server.py tests/test_codex_app_server.py tests/fixtures/codex_app_server/initialize_then_thread_start.jsonl
git commit -m "feat: add CodexAppServerClient JSON-RPC subprocess wrapper"
```

### Task 3.2: Auto-approve approval RPCs

**Files:**
- Create: `tests/fixtures/codex_app_server/with_approval_request.jsonl`
- Modify: `tests/test_codex_app_server.py`

- [ ] **Step 1: Create fixture with server-initiated approval**

Create `tests/fixtures/codex_app_server/with_approval_request.jsonl`:
```jsonl
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"codex","version":"0.133.0"},"capabilities":{}}}
{"jsonrpc":"2.0","id":2,"result":{"threadId":"th-1"}}
{"jsonrpc":"2.0","id":100,"method":"execCommandApproval","params":{"threadId":"th-1","requestId":"r1","summary":"rm -rf /tmp/x"}}
{"jsonrpc":"2.0","method":"turn/completed","params":{"threadId":"th-1","turnId":"t1","status":"completed"}}
```

- [ ] **Step 2: Add test**

Append to `tests/test_codex_app_server.py`:
```python
@pytest.mark.asyncio
async def test_auto_approve_exec_command_approval() -> None:
    """Approval requests from the server get auto-approved."""
    sent_lines: list[str] = []

    # Capture stdin writes via a custom command that echoes them to a file we read.
    # Simpler: use a real subprocess where stdin reads are visible via /dev/stderr.
    cmd = [
        "bash",
        "-c",
        (
            f"cat {FIXTURE_DIR / 'with_approval_request.jsonl'!s}; "
            "while IFS= read -r line; do echo \"GOT:$line\" >&2; done"
        ),
    ]
    notifications: list[dict] = []

    async def on_notif(n: dict) -> None:
        notifications.append(n)

    client = CodexAppServerClient(command=cmd, on_notification=on_notif)
    async with client:
        await client.initialize(client_name="t", client_version="0")
        await client.thread_start(cwd="/tmp", model=None)
        await asyncio.sleep(0.3)  # let server-request be processed

    # Read what client wrote to stdin from process stderr (captured).
    assert client._proc is not None
    stderr_bytes = await client._proc.stderr.read()  # type: ignore[union-attr]
    stderr = stderr_bytes.decode()
    # Expected: one of the GOT:... lines contains the approval response.
    assert '"decision": "approve"' in stderr or '"decision":"approve"' in stderr
```

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_codex_app_server.py::test_auto_approve_exec_command_approval -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/codex_app_server/with_approval_request.jsonl tests/test_codex_app_server.py
git commit -m "test: verify CodexAppServerClient auto-approves approval RPCs"
```

### Task 3.3: Handle subprocess EOF (CodexEof on pending requests)

**Files:**
- Create: `tests/fixtures/codex_app_server/eof_after_init.jsonl`
- Modify: `tests/test_codex_app_server.py`

- [ ] **Step 1: Create fixture (just initialize response, then EOF)**

Create `tests/fixtures/codex_app_server/eof_after_init.jsonl`:
```jsonl
{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"codex","version":"0.133.0"},"capabilities":{}}}
```

- [ ] **Step 2: Add test**

Append to `tests/test_codex_app_server.py`:
```python
from telegram_bot.core.services.codex_app_server import CodexEof


@pytest.mark.asyncio
async def test_pending_request_fails_with_eof() -> None:
    cmd = ["bash", "-c", f"cat {FIXTURE_DIR / 'eof_after_init.jsonl'!s}"]
    client = CodexAppServerClient(command=cmd, on_notification=lambda n: None)
    async with client:
        await client.initialize(client_name="t", client_version="0")
        with pytest.raises(CodexEof):
            # thread_start has no fixture response — subprocess closes stdout
            # after printing only the initialize reply, so this pending future
            # is resolved with CodexEof.
            await client.thread_start(cwd="/tmp", model=None)
```

- [ ] **Step 3: Run test**

Run: `uv run pytest tests/test_codex_app_server.py::test_pending_request_fails_with_eof -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/codex_app_server/eof_after_init.jsonl tests/test_codex_app_server.py
git commit -m "test: verify pending requests fail with CodexEof on subprocess exit"
```

---

## Phase 4: CodexDaemonManager (singleton ensure-running)

The daemon is one process per `$CODEX_HOME`. Sessions share it.

### Task 4.1: Detect and start daemon

**Files:**
- Create: `src/telegram_bot/core/services/codex_daemon.py`
- Create: `tests/test_codex_daemon.py`

- [ ] **Step 1: Discover daemon command**

Run (during implementation, output goes into the code below):
```bash
codex app-server daemon --help
codex app-server daemon start --help
```
Record the exact invocation for `daemon start` and what file/socket it creates. Default expected path: `~/.codex/app-server-control/app-server-control.sock`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_codex_daemon.py`:
```python
"""Tests for CodexDaemonManager."""
from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from telegram_bot.core.services.codex_daemon import CodexDaemonManager


def _make_socket_at(path: Path) -> socket.socket:
    """Create a listening AF_UNIX socket at `path` (for test only)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


@pytest.mark.asyncio
async def test_ensure_running_when_socket_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "app-server-control.sock"
        sock = _make_socket_at(sock_path)
        try:
            mgr = CodexDaemonManager(socket_path=sock_path, start_command=["true"])
            with patch.object(mgr, "_spawn_daemon", new_callable=AsyncMock) as spawn:
                await mgr.ensure_running()
                spawn.assert_not_called()
        finally:
            sock.close()


@pytest.mark.asyncio
async def test_ensure_running_spawns_when_socket_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sock_path = Path(tmp) / "missing.sock"
        mgr = CodexDaemonManager(socket_path=sock_path, start_command=["true"])
        with patch.object(mgr, "_spawn_daemon", new_callable=AsyncMock) as spawn:
            # _wait_for_socket returns False (we never create the socket),
            # so ensure_running should raise after the timeout.
            with pytest.raises(RuntimeError, match="daemon did not appear"):
                await mgr.ensure_running(timeout_sec=0.5)
            spawn.assert_called_once()
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_codex_daemon.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Write implementation**

Create `src/telegram_bot/core/services/codex_daemon.py`:
```python
"""Manage the lifecycle of the singleton `codex app-server` daemon.

The daemon is one process per `$CODEX_HOME`. We detect it by attempting
to connect to its Unix domain socket; if absent, we start it and wait
up to `timeout_sec` for the socket to appear.

Healthcheck is owned by `CodexSessionManager` (it can issue a cheap RPC
via any active client and treat failures as daemon trouble).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

logger = logging.getLogger("codex_daemon")

DEFAULT_SOCKET_PATH = Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
DEFAULT_START_COMMAND: list[str] = ["codex", "app-server", "daemon", "start"]


class CodexDaemonManager:
    def __init__(
        self,
        *,
        socket_path: Path = DEFAULT_SOCKET_PATH,
        start_command: list[str] = DEFAULT_START_COMMAND,
    ) -> None:
        self._socket_path = socket_path
        self._start_command = start_command

    def is_running(self) -> bool:
        """Heuristic: socket file exists AND a connection succeeds."""
        if not self._socket_path.exists():
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(str(self._socket_path))
            sock.close()
            return True
        except OSError:
            return False

    async def ensure_running(self, timeout_sec: float = 10.0) -> None:
        if self.is_running():
            return
        logger.info("starting codex app-server daemon")
        await self._spawn_daemon()
        if not await self._wait_for_socket(timeout_sec):
            raise RuntimeError(
                f"codex daemon did not appear at {self._socket_path} "
                f"within {timeout_sec}s"
            )

    async def _spawn_daemon(self) -> None:
        # Use DEVNULL so the daemon's own logs don't blast our stderr.
        proc = await asyncio.create_subprocess_exec(
            *self._start_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        # `daemon start` should fork and exit promptly; wait briefly.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("daemon start command did not exit in 5s")

    async def _wait_for_socket(self, timeout_sec: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if self.is_running():
                return True
            await asyncio.sleep(0.2)
        return False
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_codex_daemon.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/codex_daemon.py tests/test_codex_daemon.py
git commit -m "feat: add CodexDaemonManager for ensure-running + socket health"
```

---

## Phase 5: Event mapping (notification → StreamEvent)

Pure function — easy to test exhaustively.

### Task 5.1: Add `image_message` to StreamEvent

**Files:**
- Modify: `src/telegram_bot/core/services/cc_events.py`

- [ ] **Step 1: Find existing StreamEvent and extend the Literal**

Open `src/telegram_bot/core/services/cc_events.py` and locate (around line 234):
```python
@dataclass
class StreamEvent:
    """One event from CC stream."""

    type: Literal["status", "text", "result", "result_message"]
    content: str
    session_id: str | None = None
```

Replace with:
```python
@dataclass
class StreamEvent:
    """One event from a Claude/Codex agent stream.

    `image_message` carries a local filesystem path in `content` (so the
    Telegram handler can call `sendPhoto`); `session_id` is repurposed
    as the caption when the type is `image_message`.
    """

    type: Literal["status", "text", "result", "result_message", "image_message"]
    content: str
    session_id: str | None = None
```

- [ ] **Step 2: Run baseline tests to confirm no regressions**

```bash
uv run pytest -q
uv run mypy src/
uv run ruff check .
```
Expected: green.

- [ ] **Step 3: Commit**

```bash
git add src/telegram_bot/core/services/cc_events.py
git commit -m "feat: add image_message type to StreamEvent"
```

### Task 5.2: Implement `parse_codex_notification`

**Files:**
- Create: `src/telegram_bot/core/services/codex_events.py`
- Create: `tests/test_codex_events.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_codex_events.py`:
```python
"""Coverage of codex notification -> StreamEvent mapping."""
from __future__ import annotations

from telegram_bot.core.services.codex_events import parse_codex_notification


def _notif(method: str, **params: object) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def test_agent_message_delta_emits_text() -> None:
    events = parse_codex_notification(
        _notif("item/agentMessage/delta", threadId="t", itemId="i", delta="hi ")
    )
    assert [(e.type, e.content) for e in events] == [("text", "hi ")]


def test_agent_message_final_emits_result_message() -> None:
    item = {
        "id": "i",
        "type": "agentMessage",
        "text": "done.",
        "phase": "final_answer",
        "status": "completed",
    }
    events = parse_codex_notification(
        _notif("item/completed", threadId="t", item=item)
    )
    assert [(e.type, e.content) for e in events] == [("result_message", "done.")]


def test_command_execution_started_emits_status() -> None:
    item = {
        "id": "i",
        "type": "commandExecution",
        "command": "ls",
        "status": "in_progress",
    }
    events = parse_codex_notification(
        _notif("item/started", threadId="t", item=item)
    )
    assert events == []  # no event on start, only on completion failure
    # ^ revise per spec: spec says start emits "Bash: <cmd>"; choose one and align spec.


def test_command_execution_completed_nonzero_emits_status() -> None:
    item = {
        "id": "i",
        "type": "commandExecution",
        "command": "false",
        "status": "completed",
        "exitCode": 1,
    }
    events = parse_codex_notification(
        _notif("item/completed", threadId="t", item=item)
    )
    assert [(e.type, e.content) for e in events] == [
        ("status", "Bash: false (exit 1)")
    ]


def test_image_generation_completed_emits_image_message() -> None:
    item = {
        "id": "i",
        "type": "imageGeneration",
        "status": "completed",
        "savedPath": "/tmp/cat.png",
        "revisedPrompt": "a cat coding",
    }
    events = parse_codex_notification(
        _notif("item/completed", threadId="t", item=item)
    )
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_codex_events.py -v`
Expected: ImportError.

- [ ] **Step 3: Write implementation**

Create `src/telegram_bot/core/services/codex_events.py`:
```python
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
```

- [ ] **Step 4: Reconcile spec vs test — `item/started` for commandExecution**

The spec's table says `item/started commandExecution → status "Bash: <cmd>"`. The test I wrote above says `events == []` on start. **Update the test** to match the spec (and the code does emit on start now):
```python
def test_command_execution_started_emits_status() -> None:
    item = {
        "id": "i",
        "type": "commandExecution",
        "command": "ls",
        "status": "in_progress",
    }
    events = parse_codex_notification(
        _notif("item/started", threadId="t", item=item)
    )
    assert [(e.type, e.content) for e in events] == [("status", "Bash: ls")]
```

- [ ] **Step 5: Run tests + types**

```bash
uv run pytest tests/test_codex_events.py -v
uv run mypy src/telegram_bot/core/services/codex_events.py tests/test_codex_events.py
uv run ruff check src/telegram_bot/core/services/codex_events.py tests/test_codex_events.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/codex_events.py tests/test_codex_events.py
git commit -m "feat: add codex notification -> StreamEvent parser"
```

---

## Phase 6: Image delivery in handlers

`image_message` events come out of the parser. Now the streaming handler
must turn them into a real `sendPhoto` Bot API call.

### Task 6.1: Add image send helper

**Files:**
- Modify: `src/telegram_bot/core/services/telegram_utils.py` (or create a small helper if more appropriate; inspect first)

- [ ] **Step 1: Inspect existing image-send code**

```bash
grep -nE "sendPhoto|InputMediaPhoto|send_photo" src/telegram_bot/core/services/*.py src/telegram_bot/core/handlers/*.py
```
Reuse what's there; the spec mentions `_IMAGE_EXTENSIONS` and `_MAX_PHOTO_SIZE` already live in `mcp-servers/bot/server.py`. Mirror the same fallback (photo → document if >10 MB).

- [ ] **Step 2: Write the failing test for an `image_message`-aware handler helper**

Create `tests/test_image_message_handler.py`:
```python
"""StreamEvent('image_message', ...) is sent as a Telegram photo."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.handlers.streaming import dispatch_image_event
from telegram_bot.core.services.cc_events import StreamEvent


@pytest.mark.asyncio
async def test_dispatch_image_event_sends_photo(tmp_path: Path) -> None:
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n" + b"\0" * 6000)  # > 5 KB
    bot = AsyncMock()
    event = StreamEvent("image_message", str(img), "caption")
    await dispatch_image_event(
        bot=bot, chat_id=-1003, thread_id=42, event=event
    )
    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs["chat_id"] == -1003
    assert kwargs["message_thread_id"] == 42
    assert kwargs["caption"] == "caption"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_image_message_handler.py -v`
Expected: ImportError (function doesn't exist yet).

- [ ] **Step 4: Implement `dispatch_image_event`**

Add to the top of `src/telegram_bot/core/handlers/streaming.py`:
```python
from pathlib import Path

from aiogram import Bot
from aiogram.types import BufferedInputFile, FSInputFile

from telegram_bot.core.services.cc_events import StreamEvent

_MAX_PHOTO_BYTES = 10 * 1024 * 1024


async def dispatch_image_event(
    *,
    bot: Bot,
    chat_id: int,
    thread_id: int | None,
    event: StreamEvent,
) -> None:
    """Send an image_message StreamEvent as a Telegram photo (or document fallback)."""
    path = Path(event.content)
    if not path.exists():
        return
    caption = event.session_id  # repurposed field for image_message
    size = path.stat().st_size
    if size <= _MAX_PHOTO_BYTES:
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(str(path)),
            caption=caption,
            message_thread_id=thread_id,
        )
        return
    await bot.send_document(
        chat_id=chat_id,
        document=FSInputFile(str(path)),
        caption=caption,
        message_thread_id=thread_id,
    )
```

- [ ] **Step 5: Run test**

Run: `uv run pytest tests/test_image_message_handler.py -v`
Expected: PASS.

- [ ] **Step 6: Wire `dispatch_image_event` into the existing on_event callback chain**

Find the streaming on_event handler in `src/telegram_bot/core/handlers/streaming.py` (search for `event.type == "result_message"`). Add a branch:
```python
elif event.type == "image_message":
    await dispatch_image_event(
        bot=bot, chat_id=chat_id, thread_id=thread_id, event=event
    )
```

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest -q
uv run mypy src/telegram_bot/core/handlers/streaming.py
```
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/telegram_bot/core/handlers/streaming.py tests/test_image_message_handler.py
git commit -m "feat: deliver codex image_message events as Telegram photos"
```

---

## Phase 7: CodexSessionManager

The orchestrator. Implements `SessionBackend`. One `CodexAppServerClient`
per channel. Drives initialize → thread/start → turn/start → stream events.

### Task 7.1: start_session + send_stream happy path

**Files:**
- Create: `src/telegram_bot/core/services/codex_session_manager.py`
- Create: `tests/test_codex_session_manager.py`

- [ ] **Step 1: Failing test**

Create `tests/test_codex_session_manager.py`:
```python
"""High-level CodexSessionManager — happy path tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_session_manager import CodexSessionManager


@pytest.fixture
def daemon_mock() -> AsyncMock:
    m = AsyncMock(spec=CodexDaemonManager)
    m.ensure_running = AsyncMock()
    return m


@pytest.mark.asyncio
async def test_start_session_then_send_stream(daemon_mock, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],  # stub; overridden by client mock
    )

    captured: list[StreamEvent] = []

    async def on_event(ev: StreamEvent) -> None:
        captured.append(ev)

    # We monkeypatch the client to avoid spawning real subprocesses here.
    class FakeClient:
        thread_id = "th-1"

        def __init__(self, *a, **kw):
            self._on_notification = kw["on_notification"]

        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def initialize(self, **kw): return {"serverInfo": {}}
        async def thread_start(self, *, cwd, model): return {"threadId": self.thread_id}
        async def turn_start(self, *, thread_id, prompt):
            await self._on_notification({
                "method": "item/agentMessage/delta",
                "params": {"threadId": thread_id, "itemId": "i", "delta": "Hi!"},
            })
            await self._on_notification({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "item": {"id": "i", "type": "agentMessage",
                             "text": "Hi!", "phase": "final_answer",
                             "status": "completed"},
                },
            })
            await self._on_notification({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turnId": "t1",
                           "status": "completed"},
            })
        async def close(self): pass

    mgr._client_factory = FakeClient  # type: ignore[assignment]

    channel = (-1001, 99)
    await mgr.start_session(channel, cwd="/tmp", resume=False)
    await mgr.send_stream(channel, "hello", on_event)

    types = [e.type for e in captured]
    assert "text" in types
    assert "result_message" in types
    assert "result" in types
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_codex_session_manager.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement**

Create `src/telegram_bot/core/services/codex_session_manager.py`:
```python
"""Codex session orchestrator.

Owns one CodexAppServerClient per Telegram channel. Drives:

- initialize + thread/start on first message in a topic
- thread/resume on bot restart
- turn/start per user message; streams events through `on_event`
- turn/interrupt on cancel
- subprocess teardown on close_buffer/kill

Implements the slice of SessionBackend that handlers depend on.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from telegram_bot.core.services.cc_events import StreamEvent
from telegram_bot.core.services.codex_app_server import CodexAppServerClient
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_events import parse_codex_notification
from telegram_bot.core.types import ChannelKey

logger = logging.getLogger("codex_session_manager")

DEFAULT_PROXY_COMMAND: list[str] = ["codex", "app-server", "proxy"]


class CodexSessionState:
    __slots__ = ("channel_key", "thread_id", "cwd", "client", "current_turn_id",
                 "is_processing", "is_active")

    def __init__(self, *, channel_key: ChannelKey, thread_id: str, cwd: str,
                 client: CodexAppServerClient) -> None:
        self.channel_key = channel_key
        self.thread_id = thread_id
        self.cwd = cwd
        self.client = client
        self.current_turn_id: str | None = None
        self.is_processing = False
        self.is_active = True


class CodexSessionManager:
    def __init__(
        self,
        *,
        daemon: CodexDaemonManager,
        state_path: Path,
        proxy_command_factory: Callable[[], list[str]] = lambda: list(DEFAULT_PROXY_COMMAND),
    ) -> None:
        self._daemon = daemon
        self._state_path = state_path
        self._proxy_command_factory = proxy_command_factory
        self._sessions: dict[ChannelKey, CodexSessionState] = {}
        # Tests override; production uses real client.
        self._client_factory = CodexAppServerClient

    # ---- SessionBackend slice -----------------------------------------

    def is_active(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return bool(s and s.is_active)

    def is_processing(self, channel_key: ChannelKey) -> bool:
        s = self._sessions.get(channel_key)
        return bool(s and s.is_processing)

    def is_tailing(self, channel_key: ChannelKey) -> bool:
        return self.is_processing(channel_key)

    def get_session_id(self, channel_key: ChannelKey) -> str | None:
        s = self._sessions.get(channel_key)
        return s.thread_id if s else None

    def get_session_name(self, channel_key: ChannelKey) -> str | None:
        return self.get_session_id(channel_key)

    def get_provider_model(self, channel_key: ChannelKey) -> tuple[str | None, str | None]:
        return ("codex", None)

    async def start_session(
        self,
        channel_key: ChannelKey,
        *,
        cwd: str,
        resume: bool,
        session_id: str | None = None,
        model: str | None = None,
        mcp_config: str | None = None,
    ) -> None:
        await self._daemon.ensure_running()
        notif_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def on_notification(notif: dict[str, Any]) -> None:
            await notif_queue.put(notif)

        client = self._client_factory(
            command=self._proxy_command_factory(),
            on_notification=on_notification,
        )
        await client.__aenter__()
        await client.initialize(client_name="telegram-ai-agent", client_version="1.0")

        if resume and session_id:
            try:
                await client.thread_resume(thread_id=session_id)
                thread_id = session_id
            except Exception:
                logger.warning("thread_resume failed for %s; starting fresh", session_id)
                ts = await client.thread_start(cwd=cwd, model=model)
                thread_id = ts["threadId"]
        else:
            ts = await client.thread_start(cwd=cwd, model=model)
            thread_id = ts["threadId"]

        state = CodexSessionState(
            channel_key=channel_key,
            thread_id=thread_id,
            cwd=cwd,
            client=client,
        )
        self._sessions[channel_key] = state
        # Stash queue for send_stream to drain.
        state._notif_queue = notif_queue  # type: ignore[attr-defined]
        self._persist()

    async def send_stream(
        self,
        channel_key: ChannelKey,
        prompt: str,
        on_event: Callable[[StreamEvent], Awaitable[None] | None],
    ) -> str:
        state = self._sessions[channel_key]
        state.is_processing = True
        notif_queue: asyncio.Queue[dict[str, Any]] = state._notif_queue  # type: ignore[attr-defined]
        try:
            await state.client.turn_start(thread_id=state.thread_id, prompt=prompt)
            while True:
                try:
                    notif = await asyncio.wait_for(notif_queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    logger.warning("turn timeout; interrupting")
                    if state.current_turn_id:
                        await state.client.turn_interrupt(
                            thread_id=state.thread_id, turn_id=state.current_turn_id
                        )
                    break
                # Track turn id from turn/started.
                if notif.get("method") == "turn/started":
                    state.current_turn_id = notif.get("params", {}).get("turnId")
                events = parse_codex_notification(notif)
                for ev in events:
                    ret = on_event(ev)
                    if asyncio.iscoroutine(ret):
                        await ret
                    if ev.type == "result":
                        return ""
        finally:
            state.is_processing = False
            state.current_turn_id = None
        return ""

    async def send_direct(self, channel_key: ChannelKey, prompt: str) -> bool:
        # send_direct is the non-streaming variant in tmux_manager; for codex
        # we just call send_stream with a discard callback.
        async def discard(ev: StreamEvent) -> None:
            pass

        await self.send_stream(channel_key, prompt, discard)
        return True

    async def close_buffer(self, channel_key: ChannelKey) -> None:
        state = self._sessions.pop(channel_key, None)
        if state:
            state.is_active = False
            await state.client.close()
            self._persist()

    async def cancel(self, channel_key: ChannelKey) -> None:
        state = self._sessions.get(channel_key)
        if not state or not state.current_turn_id:
            return
        await state.client.turn_interrupt(
            thread_id=state.thread_id, turn_id=state.current_turn_id
        )

    async def kill(self, channel_key: ChannelKey) -> None:
        await self.close_buffer(channel_key)

    async def clear_context(self, channel_key: ChannelKey, session_manager: object) -> bool:
        # codex "clear context" = start a fresh thread, drop the old one.
        state = self._sessions.get(channel_key)
        if not state:
            return False
        cwd = state.cwd
        await self.close_buffer(channel_key)
        await self.start_session(channel_key, cwd=cwd, resume=False)
        return True

    async def switch_session(
        self, channel_key: ChannelKey, *, cwd: str, session_id: str
    ) -> None:
        await self.close_buffer(channel_key)
        await self.start_session(
            channel_key, cwd=cwd, resume=True, session_id=session_id
        )

    async def switch_or_start_session(
        self, channel_key: ChannelKey, *, cwd: str, session_id: str | None
    ) -> None:
        if session_id:
            await self.switch_session(channel_key, cwd=cwd, session_id=session_id)
        else:
            if channel_key in self._sessions:
                await self.close_buffer(channel_key)
            await self.start_session(channel_key, cwd=cwd, resume=False)

    # ---- persistence ---------------------------------------------------

    def _persist(self) -> None:
        try:
            existing = json.loads(self._state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            existing = {}
        existing.setdefault("codex_sessions", {})
        existing["codex_sessions"] = {
            f"{k[0]}:{k[1]}": {"thread_id": s.thread_id, "cwd": s.cwd}
            for k, s in self._sessions.items()
        }
        self._state_path.write_text(json.dumps(existing, indent=2))

    def restore_all(self) -> None:
        """Load persisted codex sessions; lazy reconnect on next send_stream."""
        try:
            data = json.loads(self._state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return
        for key_str, info in (data.get("codex_sessions") or {}).items():
            chat_s, thread_s = key_str.split(":")
            chat = int(chat_s)
            thread = None if thread_s == "None" else int(thread_s)
            # Defer client creation until first send; just remember thread_id.
            self._sessions[(chat, thread)] = CodexSessionState(
                channel_key=(chat, thread),
                thread_id=info["thread_id"],
                cwd=info["cwd"],
                client=None,  # type: ignore[arg-type]
            )
            self._sessions[(chat, thread)].is_active = False  # not ready until reconnect
```

- [ ] **Step 4: Run test**

Run: `uv run pytest tests/test_codex_session_manager.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/services/codex_session_manager.py tests/test_codex_session_manager.py
git commit -m "feat: add CodexSessionManager (happy path)"
```

### Task 7.2: send_stream with image_message event

**Files:**
- Modify: `tests/test_codex_session_manager.py`

- [ ] **Step 1: Add test**

Append a test that scripts an `imageGeneration` item/completed and asserts the manager forwards an `image_message` StreamEvent. Use the same `FakeClient` pattern. Code is straightforward:
```python
@pytest.mark.asyncio
async def test_send_stream_emits_image_message(daemon_mock, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")
    mgr = CodexSessionManager(
        daemon=daemon_mock,
        state_path=state_path,
        proxy_command_factory=lambda: ["bash", "-c", "cat"],
    )

    captured: list[StreamEvent] = []

    async def on_event(ev: StreamEvent) -> None:
        captured.append(ev)

    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG" + b"\0" * 6000)

    class FakeClient:
        def __init__(self, *a, **kw):
            self._on_notification = kw["on_notification"]

        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def initialize(self, **kw): return {"serverInfo": {}}
        async def thread_start(self, *, cwd, model): return {"threadId": "t"}
        async def turn_start(self, *, thread_id, prompt):
            await self._on_notification({
                "method": "item/completed",
                "params": {"threadId": thread_id,
                           "item": {"id": "i", "type": "imageGeneration",
                                    "status": "completed",
                                    "savedPath": str(img),
                                    "revisedPrompt": "cat"}},
            })
            await self._on_notification({
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turnId": "t1",
                           "status": "completed"},
            })
        async def close(self): pass

    mgr._client_factory = FakeClient  # type: ignore[assignment]
    ch = (-1, 1)
    await mgr.start_session(ch, cwd="/tmp", resume=False)
    await mgr.send_stream(ch, "draw a cat", on_event)

    assert any(e.type == "image_message" and e.content == str(img) for e in captured)
```

- [ ] **Step 2: Run + commit**

```bash
uv run pytest tests/test_codex_session_manager.py -v
git add tests/test_codex_session_manager.py
git commit -m "test: CodexSessionManager forwards image_message events"
```

### Task 7.3: cancel + clear_context + restart-then-resume

**Files:**
- Modify: `tests/test_codex_session_manager.py`

- [ ] **Step 1: Add tests for cancel, clear_context, restore_all**

Pattern:
- `cancel()` calls `turn_interrupt` on the active turn (script `turn/started` first, then await cancel, then assert the FakeClient saw the interrupt).
- `clear_context()` drops and re-creates the session (assert `thread_id` changed).
- `restore_all()` reads persisted state and a follow-up `send_stream` reconnects the client (mock `_client_factory` to track that it was called on the persisted thread_id with `resume=True`).

Write the tests, run them red, add the minimum code needed in `CodexSessionManager` to satisfy them (most of the logic is already in the implementation above; for restore-then-send, add lazy reconnection inside `send_stream` if `state.client is None`).

- [ ] **Step 2: Implement lazy reconnect in send_stream**

If `state.client is None`, call `start_session(channel_key, cwd=state.cwd, resume=True, session_id=state.thread_id)` before proceeding. Re-fetch the state object after.

- [ ] **Step 3: Commit**

```bash
git add src/telegram_bot/core/services/codex_session_manager.py tests/test_codex_session_manager.py
git commit -m "feat: CodexSessionManager cancel/clear_context/restore_all"
```

---

## Phase 8: Persistence schema

`tmux_state.py` is the existing JSON state store. We add a `codex_sessions`
top-level key (the manager already writes to it; here we formalise the
read path and dataclass).

### Task 8.1: Add CodexSessionState to tmux_state.py

**Files:**
- Modify: `src/telegram_bot/core/services/tmux_state.py`
- Create: `tests/test_codex_state_persistence.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_codex_state_persistence.py`:
```python
"""StateStore round-trips codex_sessions."""
from __future__ import annotations

import json
from pathlib import Path

from telegram_bot.core.services.tmux_state import (
    CodexSessionRecord,
    StateStore,
    load_codex_sessions,
)


def test_round_trip(tmp_path: Path) -> None:
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps({
        "codex_sessions": {
            "-1001:42": {"thread_id": "th-1", "cwd": "/home/x"},
        }
    }))

    records = load_codex_sessions(sp)
    assert records == {
        (-1001, 42): CodexSessionRecord(thread_id="th-1", cwd="/home/x"),
    }


def test_missing_key_returns_empty(tmp_path: Path) -> None:
    sp = tmp_path / "empty.json"
    sp.write_text("{}")
    assert load_codex_sessions(sp) == {}
```

- [ ] **Step 2: Run red, implement**

Add to `src/telegram_bot/core/services/tmux_state.py` near the existing dataclasses:
```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexSessionRecord:
    thread_id: str
    cwd: str


def load_codex_sessions(state_path: Path) -> dict[tuple[int, int | None], CodexSessionRecord]:
    try:
        data = json.loads(state_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    raw = data.get("codex_sessions") or {}
    result: dict[tuple[int, int | None], CodexSessionRecord] = {}
    for key_str, info in raw.items():
        chat_s, thread_s = key_str.split(":")
        chat = int(chat_s)
        thread: int | None = None if thread_s == "None" else int(thread_s)
        result[(chat, thread)] = CodexSessionRecord(
            thread_id=info["thread_id"], cwd=info["cwd"]
        )
    return result
```

- [ ] **Step 3: Test + commit**

```bash
uv run pytest tests/test_codex_state_persistence.py -v
git add src/telegram_bot/core/services/tmux_state.py tests/test_codex_state_persistence.py
git commit -m "feat: persist codex_sessions in StateStore"
```

### Task 8.2: Wire CodexSessionManager to use load_codex_sessions

**Files:**
- Modify: `src/telegram_bot/core/services/codex_session_manager.py`

Replace the inline JSON-read in `restore_all` with `load_codex_sessions(self._state_path)`. Run tests, commit:
```bash
uv run pytest tests/test_codex_session_manager.py tests/test_codex_state_persistence.py -v
git add src/telegram_bot/core/services/codex_session_manager.py
git commit -m "refactor: codex restore_all uses load_codex_sessions"
```

---

## Phase 9: Strip legacy codex from existing files

### Task 9.1: Remove legacy from CodexAdapter

**Files:**
- Modify: `src/telegram_bot/core/services/providers.py`

- [ ] **Step 1: Delete `build_tui_start`, `build_tui_resume`, `parse_tui_event`, `find_tui_transcript`, `locate_tui_transcript`, `transcript_path_for_state`, `_codex_tui_prefix`, modal helpers used only by tmux path.**

- [ ] **Step 2: Run tests**

`uv run pytest -q && uv run mypy src/`
If imports of removed symbols exist elsewhere, fix them as part of the next tasks.

- [ ] **Step 3: Commit**

```bash
git add src/telegram_bot/core/services/providers.py
git commit -m "refactor: strip rollout/TUI logic from CodexAdapter"
```

### Task 9.2: Remove codex branches from tmux_manager

**Files:**
- Modify: `src/telegram_bot/core/services/tmux_manager.py`

- [ ] **Step 1: Search for `"codex"` and `provider == "codex"` in tmux_manager.py and remove every branch. Methods like `_locate_codex_transcript_after_send`, `_codex_start_snapshots`, `_codex_transcript_for_state`, `_find_codex_transcript` get deleted in full.**

- [ ] **Step 2: Same for `tmux_recovery.py`.**

- [ ] **Step 3: Run full suite**

`uv run pytest -q && uv run mypy src/`

Expected: green. Fix any compile errors in handlers introduced by removing codex branches — they should not have referenced the codex-specific methods, but if they did, the next phase wires the codex backend in their place.

- [ ] **Step 4: Commit**

```bash
git add src/telegram_bot/core/services/tmux_manager.py src/telegram_bot/core/services/tmux_recovery.py
git commit -m "refactor: drop codex branches from tmux_manager (claude-only now)"
```

---

## Phase 10: Handler dispatch by engine

Now wire handlers to call through `BackendDispatcher` instead of
`tmux_manager` directly.

### Task 10.1: streaming.py route by engine

**Files:**
- Modify: `src/telegram_bot/core/handlers/streaming.py`

- [ ] **Step 1: Locate all `tmux_manager.<method>` calls in streaming.py.**

Run: `grep -nE "tmux_manager\." src/telegram_bot/core/handlers/streaming.py`

- [ ] **Step 2: For each call, derive the engine from the topic_config:**

Add helper near top:
```python
def _backend_for(
    dispatcher: BackendDispatcher,
    topic_config: object,
    channel_key: ChannelKey,
) -> SessionBackend:
    chat_id, thread_id = channel_key
    engine = topic_config.get_topic(thread_id).engine if thread_id is not None else "claude"
    return dispatcher.for_engine(engine)
```

Replace each `tmux_manager.foo(key, ...)` with:
```python
backend = _backend_for(dispatcher, topic_config, key)
await backend.foo(key, ...)
```

The streaming function should take `dispatcher` instead of `tmux_manager` directly. Update its signature and all callers in this file. Defer call-site update in `__main__.py` to Phase 11.

- [ ] **Step 3: Run tests + types**

`uv run pytest -q && uv run mypy src/telegram_bot/core/handlers/streaming.py`

- [ ] **Step 4: Commit**

```bash
git add src/telegram_bot/core/handlers/streaming.py
git commit -m "refactor: streaming.py routes through BackendDispatcher"
```

### Task 10.2–10.6: Same refactor for _dispatch.py, text.py, cancel.py, commands.py, forum_topic.py

For each file:
- [ ] Search for `tmux_manager.` references
- [ ] Replace with `dispatcher.for_engine(engine).<method>`
- [ ] Update function signatures to accept `dispatcher: BackendDispatcher`
- [ ] Run `uv run pytest -q && uv run mypy src/<file>`
- [ ] Commit per file

---

## Phase 11: Wire dispatcher in __main__.py

### Task 11.1: Construct codex stack in startup

**Files:**
- Modify: `src/telegram_bot/__main__.py`

- [ ] **Step 1: Add construction near where `tmux_manager` is created:**

```python
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_session_manager import CodexSessionManager
from telegram_bot.core.services.session_backend import BackendDispatcher

# ...
codex_daemon = CodexDaemonManager()
codex_manager = CodexSessionManager(
    daemon=codex_daemon,
    state_path=tmux_manager.state_path,  # share state file
)
codex_manager.restore_all()
dispatcher = BackendDispatcher(claude=tmux_manager, codex=codex_manager)
```

- [ ] **Step 2: Pass `dispatcher` to handler-registration functions instead of `tmux_manager`.**

Trace each `register_*` call; pass dispatcher. Inside those, the calls have already been refactored in Phase 10 to use it.

- [ ] **Step 3: Run all tests + smoke compile**

```bash
uv run pytest -q
uv run mypy src/
uv run ruff check .
```
Expected: green.

- [ ] **Step 4: Commit**

```bash
git add src/telegram_bot/__main__.py
git commit -m "feat: wire BackendDispatcher (claude + codex) in startup"
```

---

## Phase 12: Smoke script

### Task 12.1: scripts/smoke-codex.sh

**Files:**
- Create: `scripts/smoke-codex.sh`

- [ ] **Step 1: Write script**

Create `scripts/smoke-codex.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail

# Manual smoke for CodexSessionManager. Talks to a real codex daemon under
# the current ChatGPT-auth account. Does not touch Telegram — drives the
# session manager directly and prints what it sees.

cd "$(dirname "$0")/.."

echo "==> codex auth status"
codex login status || { echo "Not logged in"; exit 1; }

echo "==> ensuring daemon"
codex app-server daemon start 2>/dev/null || true

WORKDIR="$(mktemp -d -t codex-smoke-XXXXXX)"
echo "==> workdir: $WORKDIR"

uv run python <<PY
import asyncio
from pathlib import Path
from telegram_bot.core.services.codex_daemon import CodexDaemonManager
from telegram_bot.core.services.codex_session_manager import CodexSessionManager
from telegram_bot.core.services.cc_events import StreamEvent

async def main() -> None:
    state = Path("$WORKDIR") / "state.json"
    state.write_text("{}")
    mgr = CodexSessionManager(daemon=CodexDaemonManager(), state_path=state)
    ch = (-1, 1)
    await mgr.start_session(ch, cwd="$WORKDIR", resume=False)

    async def on_event(ev: StreamEvent) -> None:
        print(f"[{ev.type}] {ev.content[:200]}")

    for prompt in [
        "say hello",
        "create hello.py that prints 'hi from codex', then run it",
        "what did it print?",
        "generate a 16:9 image of a cat coding",
    ]:
        print(f"\n>>> {prompt}")
        await mgr.send_stream(ch, prompt, on_event)

    # Assertions
    assert (Path("$WORKDIR") / "hello.py").exists(), "hello.py missing"
    print("\nOK")

asyncio.run(main())
PY

echo "==> cleanup"
rm -rf "$WORKDIR"
```

- [ ] **Step 2: Make executable + commit**

```bash
chmod +x scripts/smoke-codex.sh
git add scripts/smoke-codex.sh
git commit -m "test: add manual smoke script for CodexSessionManager"
```

### Task 12.2: Run the smoke

- [ ] **Step 1: Run**

```bash
./scripts/smoke-codex.sh
```

Expected: prints text events, status events for Bash, an image_message event with an existing file path, and "OK" at the end.

- [ ] **Step 2: If any test/event diverges from spec, fix the mapping in `codex_events.py` and re-run.**

---

## Phase 13: Manual end-to-end verification

### Task 13.1: Deploy branch to local bot

- [ ] **Step 1: Restart bot on the branch**

```bash
cd /home/evgen/projects/telegram-ai-agent
git rev-parse --abbrev-ref HEAD  # confirm: feat/codex-app-server-adapter
uv sync
systemctl --user restart telegram-ai-agent
journalctl --user -u telegram-ai-agent -f
```

- [ ] **Step 2: In Telegram topic `solid-tech codex` (#3364), send:**

1. «привет, кто ты?» — expect typing-stream reply ending with a final message.
2. «создай hello.py с print('hi') и запусти» — expect a Bash status event, then the run output, then a final message.
3. «сгенерь 16:9 картинку кота-кодера» — expect a `Generating image…` status, then a real photo posted in the chat.
4. `systemctl --user restart telegram-ai-agent` from another shell; then send «продолжи нашу беседу про кота» — expect codex resumes the same thread.

- [ ] **Step 3: Inspect logs for unhandled notification warnings:**

```bash
journalctl --user -u telegram-ai-agent --since "10 minutes ago" | grep -i "unhandled codex notification"
```

For each method that pops up, decide whether to handle it (extend the mapping) or silence it (add to a known-ignored list).

### Task 13.2: DoD check

- [ ] All unit tests + mypy + ruff green
- [ ] Smoke script passed end-to-end
- [ ] Manual Telegram check: text reply, Bash status, image, resume after restart — all confirmed
- [ ] The original `RuntimeError: Codex TUI transcript discovery failed` does not reproduce (grep the journal)

---

## Phase 14: Push + open PR to fork (no upstream PR yet)

### Task 14.1: Push branch to fork

- [ ] **Step 1: Push**

```bash
git remote -v | grep -q '^fork' || git remote add fork https://github.com/evgen-20/telegram-ai-bot.git
git push fork feat/codex-app-server-adapter
```

- [ ] **Step 2: Open PR fork→fork (main of `evgen-20/telegram-ai-bot`)**

```bash
gh pr create --repo evgen-20/telegram-ai-bot \
  --base main --head feat/codex-app-server-adapter \
  --title "Codex app-server adapter (replaces broken JSONL rollout path)" \
  --body "$(cat <<'EOF'
## Summary
- Replaces broken JSONL-rollout transcript reader (Codex >= 0.133 stopped writing
  those files) with a JSON-RPC client over `codex app-server proxy`.
- Adds first-class image-generation support (Codex emits
  `ImageGenerationThreadItem`; bot posts the saved file as a Telegram photo).
- Introduces `SessionBackend` Protocol + `BackendDispatcher` so handlers stay
  engine-agnostic.

## Test plan
- [x] Unit tests for client, daemon, events, session manager, persistence
- [x] mypy --strict + ruff clean
- [x] `scripts/smoke-codex.sh` passes end-to-end against real codex
- [x] Manual TG check in `solid-tech codex` topic: text, Bash, image, resume
EOF
)"
```

### Task 14.2: (deferred) Upstream PR

Not on the critical path. Open when ready: same branch, target
`pavel-molyanov/telegram-ai-agent:main`.

---

## Self-Review Notes (post-write)

Spec coverage:
- Goal 1 (restore delivery): Phase 7
- Goal 2 (parity): Phases 1, 10, 11 (SessionBackend covers handler-visible methods)
- Goal 3 (use app-server, not SQLite): Phases 3-7
- Goal 4 (image gen in TG): Phases 5.1, 6
- Goal 5 (clean fork PR): Phase 14

Out-of-scope items honoured (no tasks added for): `/tui` on codex topics,
interactive approval RPCs surfaced to user, reasoning streams, realtime
audio, Codex <0.133 support.

Known follow-ups (not blocking v1):
- Better health-ping than "next user message will tell us"
- Per-channel daemon override (CODEX_HOME) if shared daemon becomes a
  bottleneck
- `/tui` for codex via on-demand `codex resume <thread_id>` in tmux

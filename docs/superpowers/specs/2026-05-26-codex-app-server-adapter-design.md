# Codex app-server adapter — design

**Status:** approved (brainstorm)
**Date:** 2026-05-26
**Author:** evgen-20 + Claude
**Targets:** `evgen-20/telegram-ai-bot` fork; PR-candidate for `pavel-molyanov/telegram-ai-agent` upstream

## 1. Context

The bot currently delivers Codex CLI responses to Telegram by tailing JSONL
"rollout" files in `~/.codex/sessions/**/*.jsonl`. Codex 0.133+ (with ChatGPT
auth) no longer writes those files — it persists session state in SQLite
(`~/.codex/state_5.sqlite`, tables `threads`, `agent_jobs`,
`agent_job_items`, ...) and exposes a JSON-RPC app-server over the Unix
socket `~/.codex/app-server-control/app-server-control.sock`.

Observable failure: `CodexAdapter.locate_tui_transcript` times out after 30 s
and the bot emits «Codex принял сообщение, но бот не смог найти transcript
для стриминга ответа». The Codex TUI itself works fine inside `tmux` and the
user sees a real reply when attaching via `/tui` — only the bot's read path
is broken.

## 2. Goals

- Restore end-to-end delivery of Codex responses to Telegram on Codex ≥ 0.133.
- Reach feature parity with the Claude adapter: streaming text deltas, tool /
  command status messages, final answer, restart survival.
- Use Codex's documented integration surface (app-server JSON-RPC), not its
  internal SQLite schema.
- Surface Codex's native image generation (a first-class
  `ImageGenerationThreadItem` in the protocol, included in ChatGPT subscription
  — no MCP or third-party API needed) by auto-delivering the saved file to the
  Telegram chat as a photo.
- Land cleanly in the fork, ready for an upstream PR.

## 3. Non-goals (v1)

- `/tui` access for codex topics (Codex now runs in a daemon, not in tmux).
  Stub with a "not available for codex in v1" message; tracked as a follow-up.
- Interactive approval RPCs surfaced to the user (auto-approve everything,
  matching the current `--dangerously-bypass-approvals-and-sandbox` posture).
- Streaming `item/reasoning/*` events (model thinking) to Telegram.
- Realtime audio events (`thread/realtime/*`).
- Backward compatibility with Codex < 0.133 (legacy file-rollout path is
  deleted, not gated).
- **Inbound files for codex topics** (photo / forward / voice uploads).
  Photo, forward, and voice handlers currently route file-uploads through
  `tmux_manager.start_session` (or call it directly outside of the
  dispatcher path), which raises
  `TmuxManager no longer manages codex sessions; use CodexSessionManager
  instead` for any codex topic. Surfaces to the user as
  «❌ Failed to start tmux: …». Out of scope here; follow-up needs:
    1. Make `photo.py` / `forward.py` / `voice.py` route file upload
       lifecycle through `BackendDispatcher` like text already does.
    2. Implement codex-side file ingest (likely an attachment param to
       `turn/start`, or pre-uploading via `fs/writeFile` RPC).
    3. Replace the «Failed to start tmux» wording with a codex-aware
       message when the failure comes from the codex path.

## 4. Architecture

Bot ↔ Codex integration shifts from "spawn TUI in tmux, send keys, tail
rollouts" to "talk to the Codex app-server daemon via JSON-RPC".

```
+-------------------------+
| Telegram handlers       |
| (streaming, _dispatch)  |
+-----------+-------------+
            |  dispatch by engine
   +--------+--------+
   |                 |
   v                 v
TmuxSessionManager   CodexSessionManager   <-- NEW
(unchanged,          |
 claude only)        v
                  CodexAppServerClient    <-- NEW
                     |
                     v
              codex app-server proxy      (subprocess per session)
                     |
                     v Unix socket
              ~/.codex/app-server-control/app-server-control.sock
                     |
                     v
              codex app-server daemon
                     |
                     v
              ~/.codex/state_5.sqlite  (codex-internal; bot never touches)
```

The daemon is a singleton per `$CODEX_HOME`. Each Telegram session opens its
own `codex app-server proxy` subprocess that pipes stdio to the singleton
socket. Input (`turn/start`) and output (`item/agentMessage/delta`,
`turn/completed`, ...) both flow through the same RPC channel.

`tmux` is no longer involved on the codex path. The TUI is not spawned for
codex topics in v1.

## 5. Components

### 5.1 `core/services/codex_app_server.py` (new)

`CodexAppServerClient` — async JSON-RPC client wrapping one `codex
app-server proxy` subprocess.

Public surface:

- `async __aenter__` / `__aexit__` — spawn / terminate the proxy subprocess;
  ensure the daemon is reachable (start it if not).
- `async initialize() -> InitializeResult`
- `async thread_start(*, cwd: str, model: str | None) -> str` — returns
  `thread_id`.
- `async thread_resume(*, thread_id: str) -> None`
- `async turn_start(*, thread_id: str, prompt: str) -> None` — fire-and-stream
  (events arrive via the subscribed callback).
- `subscribe(callback: Callable[[ServerNotification], Awaitable[None]]) ->
  None` — register a single dispatcher; raised exceptions are logged and
  swallowed so a misbehaving handler doesn't kill the read loop.
- `async close() -> None`

Internals:

- One background `asyncio.Task` reads JSON-lines from subprocess stdout,
  parses, dispatches notifications to the subscriber, resolves request
  futures by `id`.
- Auto-respond `approve` to every server-initiated `*Approval` RPC.
- Reconnect on subprocess EOF / EPIPE: terminate, re-spawn proxy, re-issue
  `initialize` + `thread_resume`. If reconnect fails 3 times in 30 s — give up
  and surface error upstream.

Typing: payloads typed via `TypedDict`s derived from `codex app-server
generate-json-schema` output (committed to `docs/codex-protocol/` as
reference; Python types generated once and checked in).

### 5.2 `core/services/codex_session_manager.py` (new)

Orchestrator mirroring the public interface of `tmux_manager` so the
handlers stay engine-agnostic:

- `async start_session(channel_key, cwd, *, resume: bool) -> None`
- `async send_stream(channel_key, prompt, on_event: Callable) -> str`
- `async close_buffer(channel_key) -> None`
- `restore_all() -> None`
- `is_processing(channel_key) -> bool`

Owns one `CodexAppServerClient` per active channel. Translates
`ServerNotification`s into `StreamEvent`s (mapping table below) and forwards
them through the `on_event` callback passed by `streaming.py`.

Persists `{channel_key: {thread_id, cwd, started_at}}` in
`session_state.json` (extending the existing tmux state store with a new
`codex_sessions` key).

### 5.3 `core/services/providers.py::CodexAdapter` (edited)

Delete the legacy rollout path:
- `build_tui_start`, `build_tui_resume`, `parse_tui_event`,
  `find_tui_transcript`, `locate_tui_transcript`, `transcript_path_for_state`,
  `is_prompt_ready`, `is_modal_present`.
- Keep only adapter metadata (`name`, `binary()`, `supports_resume()`, etc.)
  used by non-runtime code (e.g. `/repo` UI). If empty after the cut, delete
  the adapter and its `CODEX_ADAPTER` constant entirely.

### 5.4 `core/services/tmux_manager.py` (edited)

- Remove `_locate_codex_transcript_after_send`.
- Remove `_codex_start_snapshots`, `_codex_transcript_for_state`,
  `_find_codex_transcript`.
- Remove all `state.provider == "codex"` branches; the manager is now
  claude-only.
- `restore_all` ignores codex entries in state (claude-only paths).

### 5.5 `core/handlers/streaming.py`, `core/handlers/_dispatch.py` (edited)

Add a single dispatch layer: look up the topic's engine from
`topic_config.json`. If `engine == "codex"`, route to `CodexSessionManager`;
else `TmuxSessionManager` (renamed from current `tmux_manager`, behaviour
unchanged).

### 5.6 `core/services/tmux_state.py` (edited)

Extend the JSON schema with a top-level `codex_sessions` mapping. Migration
on load: missing key → empty dict (no version bump needed). Save path
unchanged.

## 6. Event mapping (ServerNotification → StreamEvent)

| Notification method | StreamEvent emitted |
|---|---|
| `item/agentMessage/delta` | `("text", chunk)` |
| `item/completed` (kind=agentMessage, phase=final_answer) | `("result_message", full_text)` |
| `item/started` (kind=commandExecution) | `("status", "Bash: <cmd>")` |
| `item/completed` (kind=commandExecution, exit≠0) | `("status", "Bash: <cmd> (exit N)")` |
| `item/started` (kind=fileChange) | `("status", "Edit: <path>")` |
| `item/completed` (kind=fileChange, error) | `("status", "Edit: <path> failed: <err>")` |
| `item/completed` (kind=fileChange, success) | nothing (success implied; success-on-end suppression matches Bash behaviour) |
| `item/started` (kind=mcpToolCall) | `("status", "MCP: <tool>")` |
| `item/completed` (kind=mcpToolCall, error) | `("status", "MCP: <tool> failed: <err>")` |
| `item/started` (kind=imageGeneration) | `("status", "Generating image...")` |
| `item/completed` (kind=imageGeneration, success) | `("image_message", savedPath, revisedPrompt)` — bot sends photo via `sendPhoto` (fallback `sendDocument` if >10 MB) |
| `item/completed` (kind=imageGeneration, error) | `("status", "Image generation failed: <err>")` |
| `turn/completed` | `("result", "")` — done sentinel |
| `*Approval` (server→client RPC) | auto-reply `approve`; no event |
| `thread/tokenUsage/updated` | stash on session state (for `/status`); no event |
| `error` | `("result_message", "Codex error: <msg>")`; done |
| everything else | logged at DEBUG; no event |

Unknown notifications are logged at WARN once per method, then DEBUG. We
want loud diagnostics on new event types as Codex evolves.

## 7. Lifecycle

### Daemon

Lazy-start. Before the first proxy spawn, `CodexAppServerClient` checks for
the socket file; if absent or `connect()` fails, spawn `codex app-server
daemon start` (exact form discovered from `codex app-server daemon --help`,
recorded in the implementation plan), wait up to 10 s for the socket to
appear.

Health: every 60 s, the manager issues `account/read` against any active
client as a ping. Failures count toward reconnect logic.

### Session

```
start_session(channel_key, cwd):
  ensure daemon
  spawn `codex app-server proxy` subprocess
  → initialize
  → thread/start { cwd, model }      → thread_id
  persist {channel_key: thread_id} in session_state.json
  start background notification dispatcher

send_stream(channel_key, prompt):
  → turn/start { thread_id, prompt }
  forward notifications via on_event until turn/completed
  return when sentinel received

close_buffer(channel_key):
  → thread/unsubscribe
  terminate proxy subprocess
  thread remains alive in daemon (can be resumed later)
```

### Restart recovery

On bot start, `restore_all`:
- Read `codex_sessions` from `session_state.json`.
- For each entry, lazily on the next `send_stream` call: spawn proxy +
  `thread_resume`. If resume fails (thread archived / lost), drop the entry
  and start a fresh thread on first message.

### Concurrency

One in-flight `turn/start` per channel (matching existing
`_is_processing` discipline). Daemon handles cross-channel parallelism
internally — different `thread_id`s are independent.

## 8. Error handling

| Failure mode | Detection | Response |
|---|---|---|
| Daemon not running | `connect()` ECONNREFUSED / missing socket | start daemon, wait 10 s, retry |
| Daemon crashed mid-turn | subprocess EOF / EPIPE | mark idle, surface `("status", "Codex daemon restarting...")`, restart + resume + retry turn (idempotent: `turn/start` is replayable) up to 3× / 30 s |
| `thread_resume` rejected | RPC error | drop persisted thread_id, start new one |
| Unknown notification method | runtime check | log WARN once, then DEBUG; no user-facing event |
| Approval RPC handler throws | wrapped try/except | auto-reply `deny`, surface `("result_message", "Codex requested approval but bot couldn't handle it")` |
| Smoke script: real turn never emits `turn/completed` | 5 min timeout | force-cancel via `turn/interrupt`, surface error |

## 9. Testing

### Unit (in CI)

Existing layout is flat (`tests/test_*.py`). New files follow the same
convention:

`tests/test_codex_app_server.py`:
- Mock subprocess with a bash stub that prints fixture JSON-RPC frames to
  stdout on cue.
- Cover: `initialize` happy path; `thread/start` round trip; notification
  dispatch; auto-approve on `ExecCommandApproval`; reconnect after stdout
  EOF.

`tests/test_codex_session_manager.py`:
- Mock `CodexAppServerClient`. Feed scripted notification sequences from
  `tests/fixtures/codex_app_server/*.json` (real dumps captured by the smoke
  script). Assert emitted `StreamEvent` sequences.
- Scenarios: text-only turn; text + Bash; text + file edit; turn with error;
  multi-turn replay; restart-then-resume.

`tests/test_tmux_state_codex.py`:
- Round-trip `codex_sessions` persistence, including legacy state files
  (missing key → empty).

Types: `mypy --strict` clean. Ruff clean.

### Smoke (manual, local)

`scripts/smoke-codex.sh`:
1. Verify `codex auth status` is logged in.
2. Verify daemon healthy (or start it).
3. Drive the bot's `CodexSessionManager` directly (no Telegram I/O) against a
   sandbox cwd: `tests/smoke-fixtures/codex-cwd/`.
4. Run 4 turns: greeting; "create hello.py and run it"; "what did it print?";
   "generate a 16:9 image of a cat coding". Per-turn timeout: 5 minutes
   (force `turn/interrupt` and fail on overrun).
5. Assert: final messages received; status events for Bash + Write appeared;
   `hello.py` exists on disk; an `ImageGenerationThreadItem` event arrived
   with `savedPath` pointing to an existing file ≥5 KB; cleanup.

Run manually before merging to fork `main`. Not in CI (needs auth + API
spend).

## 10. Deployment

```bash
# /home/evgen/projects/telegram-ai-agent
git remote add fork https://github.com/evgen-20/telegram-ai-bot.git
git fetch fork
git checkout feat/codex-app-server-adapter
uv sync
systemctl --user restart telegram-ai-agent
journalctl --user -u telegram-ai-agent -f
```

Rollback: `git checkout main && systemctl --user restart telegram-ai-agent`.

After verification on the live `solid-tech codex` topic (#3364), open a PR
from the fork branch to `pavel-molyanov/telegram-ai-agent:main`. PR landing
is not on the critical path — fork branch is the source of truth for our
deployment.

## 11. Risks and mitigations

1. **App-server protocol changes in Codex ≥ 0.134.** Versioned schema is
   committed to `docs/codex-protocol/`; smoke script catches breakage on
   dev. Mitigation: temporarily pin Codex (`npm install -g
   @openai/codex@0.133.x`) while we adapt.
2. **Daemon hangs / deadlocks.** 10 s start timeout, 60 s health-ping,
   per-turn 5 min cancel. Stuck → `turn/interrupt` + restart.
3. **Concurrent topics serialised by daemon.** Codex daemon is expected to
   parallelise across `thread_id`s. If it doesn't, smoke script detects;
   fallback would be one daemon per channel (CODEX_HOME override), but not
   implemented in v1.
4. **`/tui` regression.** Communicated as known limitation in README + bot
   reply to `/tui` on codex topic.

## 12. Definition of Done

- Unit tests green; `mypy --strict` + `ruff` clean.
- Smoke script passes happy path + 2 edge cases.
- Manual check in topic `solid-tech codex` (#3364): message → typing-stream
  reply + visible Bash status events.
- Manual check in same topic: "сгенерь картинку X" → photo appears in chat as
  a Telegram photo (not as a document), with no OpenRouter / MCP setup
  required (relies solely on Codex's ChatGPT-account image-gen capability).
- The original `RuntimeError: Codex TUI transcript discovery failed` no
  longer reproduces.
- After `systemctl restart`, the next message in `#3364` continues the same
  thread (resume).

## 13. Open items deferred to plan / implementation

- Exact `codex app-server daemon` start invocation (read from `--help`).
- Concrete Python `TypedDict` shapes (auto-generated from JSON Schema during
  implementation; checked into `core/services/codex_protocol.py`).
- Health-ping RPC choice (`account/read` is a guess; pick the cheapest).
- Whether to delete `CodexAdapter` entirely or keep a thin metadata shell —
  decided when the implementation reveals what still references it.

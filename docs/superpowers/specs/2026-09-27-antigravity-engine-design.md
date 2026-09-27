# Antigravity CLI as a third engine — design

**Status:** approved design, awaiting implementation plan
**Date:** 2026-09-27
**Reference:** [`docs/antigravity-protocol/README.md`](../../antigravity-protocol/README.md)

## 1. Goal

Add Antigravity CLI (`agy`) as a third `engine` next to `claude` and `codex`,
usable in **both** execution modes. A topic configured with
`engine: antigravity` must behave like a Claude or Codex topic: streamed
progress, final answer, session resume, `/cancel`, `/tail`, recovery after a
bot restart, the bot's own MCP tools, and generated images delivered to the
chat automatically.

`tmux` is the primary mode — it is how project topics run in practice — and
`subprocess` is the secondary one for task/assistant topics.

**Success criteria**

- A tmux topic on `antigravity` answers a multi-step request with live tool
  status and the final answer, survives a bot restart mid-turn, and `/cancel`
  stops the turn.
- The topic continues the same `agy` conversation after its tmux session is
  restarted and via reply-to-resume, without the session id being mistaken
  for a Claude one.
- A subprocess topic on `antigravity` streams text and finishes with the final
  answer.
- `generate_image` output appears in the chat as a photo without the agent
  having to call a tool for it, and is not sent twice.
- The agent can call `mcp__bot__send_*`, and the files land in the topic the
  request came from.
- On a host without `agy`, the engine is reported as unavailable and nothing
  else changes.

## 2. Decisions

| Question | Decision |
|----------|----------|
| Execution modes | Both. tmux first, subprocess second. |
| tmux event source | `brain/<conversation_id>/.system_generated/logs/transcript_full.jsonl`, tailed by the existing `TailRunner`. Verified to grow step by step while the TUI is working. |
| subprocess protocol | One `agy -p … --output-format stream-json` process per message, resumed with `--conversation <id>`. No persistent `--input-format stream-json` process: resume works across processes and `cwd`s. |
| Architecture | A provider adapter plus a branch in the existing session paths (`tmux_manager.py` for tmux, `_build_exec_command` in `claude.py` for subprocess) — the same shape Codex already uses. No new `SessionBackend`. |
| Trust dialog | Auto-confirmed, as Codex's already is (`_await_codex_prompt_ready`). The topic `cwd` is set by the operator. |
| MCP | Register the bot MCP server once in `agy`'s global config; pass topic routing through the `agy` process environment. |
| Engine of a session id | Stored explicitly. The UUID-version heuristic (`claude.py:141`) cannot tell `agy` ids (UUIDv4) from Claude ids. |
| Automatic engine fallback | `antigravity` is never a fallback target; it is used only when a topic selects it. |
| Quota | Out of scope — no headless usage command exists. |
| Per-topic custom MCP servers for `agy` | Out of scope — only the bot server is wired. |
| Images sent by the user to the agent | Out of scope. |

## 3. Components

New code lives in its own module so `claude.py` (≈1.7k lines) and
`tmux_manager.py` (≈3.5k lines) only gain thin dispatch branches.

### 3.1 `services/antigravity.py` (new)

- **Binary resolution** — `antigravity_binary()` / `safe_antigravity_binary()`,
  mirroring `claude_binary()`: `TELEGRAM_AGY_BIN` override, then
  `~/.local/bin/agy`, then `PATH`; only executables owned by the service user
  and not group/world writable.
- **Command builders**
  - TUI start: `agy --dangerously-skip-permissions [--model M]`, run in the
    topic `cwd`.
  - TUI resume: the same plus `--conversation <id>`.
  - Exec: `agy -p <prompt> --output-format stream-json
    --dangerously-skip-permissions [--conversation <id>] [--model M]`, stdin
    closed.
- **Process environment** — `antigravity_process_env(channel_key)`: the agent
  environment plus `TELEGRAM_CHAT_ID`, `TELEGRAM_THREAD_ID`,
  `TELEGRAM_CONTEXT_LOCK=1` for the bot MCP server, and
  `AGY_CLI_DISABLE_AUTO_UPDATE=1` so the CLI cannot change version under a
  running bot.
- **Paths** — `brain_dir(conversation_id)`, `transcript_path(conversation_id)`.

### 3.2 `AntigravityAdapter` in `providers.py`

Satisfies `ProviderAdapter`, registered as `ANTIGRAVITY_ADAPTER` next to
`CODEX_ADAPTER`; `Engine` gains `"antigravity"`, `engine_display_name` returns
`Antigravity`.

- `parse_exec_event` — stream-json:
  - `init` → session id (`conversation_id`).
  - `step_update`, `step_type: agent_response`, `text_delta` → text.
  - `step_update`, `step_type: tool`, `state: ACTIVE` → status line
    (`tool_name` plus the salient parameter, e.g. `CommandLine`,
    `AbsolutePath`); `generate_image` → "Generating image…".
  - `step_update`, `step_type: tool`, `tool_name: generate_image`,
    `state: DONE` → image lookup (§3.5).
  - `result` → final answer, `status`, `usage`; `status: ERROR` → error event.
  - Non-JSON lines: `AGY_ERROR: {…}` → error event; a location/eligibility
    error gets its own message pointing at the account, not at the bot.
  - Unknown `step_type` values are ignored, never fatal.
- `parse_tui_event` — one `transcript_full.jsonl` step per line:
  - `USER_INPUT` → turn start (ignored for output).
  - `PLANNER_RESPONSE` → `content` as text; each `tool_calls[]` entry → status
    line. A `PLANNER_RESPONSE` **without** `tool_calls` ends the turn.
  - `GENERIC` → tool result: ignored for text; each `media[]` entry with an
    `image/*` MIME type → `image_message` with the local path from `uri`
    (`file://` prefix stripped when present; bare paths also occur).
- `is_prompt_ready(pane)` — the idle footer `? for shortcuts` in the live tail
  and no `esc to cancel` there.
- `is_modal_present(pane)` — trust dialog and other selection lists
  (`↑/↓ Navigate · enter Confirm` in the live tail), checked on the tail only,
  as Codex does, so scrollback does not raise false alerts.
- `transcript_path_for_state` — stored path if it exists, else the path derived
  from the session id.

### 3.3 tmux path (`tmux_manager.py`)

Engine branches where Codex already has them:

- **Start / resume** — launch the adapter's TUI command in the pane with the
  process environment from §3.1.
- **Readiness** — `_await_antigravity_prompt_ready`: poll the pane; on
  "Do you trust the contents of this project?" press Enter once (the default
  choice is "Yes, I trust this folder"), then wait for `is_prompt_ready`.
- **New-conversation discovery** — before the first prompt, snapshot
  `brain/` directory names; after the prompt is sent, the new directory that
  contains `transcript_full.jsonl` is the conversation. Its name is the
  session id. Same pattern as `CodexTranscriptSnapshot`.
- **Tail** — the existing `TailRunner` with `ANTIGRAVITY_ADAPTER.parse_tui_event`.
- **Cancel** — send `Escape` to the pane (the busy footer says `esc to cancel`;
  confirm on a live pane in stage 2 that the transcript records the aborted
  turn and the prompt returns).
- **Recovery** — resume from the stored session id and transcript path, as for
  the other engines.

### 3.4 subprocess path (`claude.py`)

`_build_exec_command` gains an `antigravity` branch built from §3.1; the
existing stream loop consumes `ANTIGRAVITY_ADAPTER.parse_exec_event`. The
session id captured from `init` is stored for the next message.

### 3.5 Generated images

A shared helper `find_generated_images(conversation_id, since_step)` reads
`transcript_full.jsonl` and returns `media[].uri` image paths from steps newer
than `since_step`, restricted to files under `brain/<conversation_id>/`.

- tmux: images come straight from `parse_tui_event` (§3.2); the helper is not
  needed.
- subprocess: on a `generate_image` `DONE` event, the helper runs against the
  conversation's transcript (the stream-json event carries no path).
- Both emit `image_message`, delivered by the existing `dispatch_image_event`.
- **De-duplication** — a per-turn set of delivered image paths; a path the
  agent also sends via `mcp__bot__send_image` in the same turn is not sent
  again.

### 3.6 Bot MCP server

- `ensure_antigravity_bot_mcp()` at bot start-up, only when `agy` is
  available: if `agy mcp list` lacks `bot`, run
  `agy mcp add -e APP_ROOT=… -e ENV_FILE=… -e PROJECT_DIR=… bot bash
  <repo>/mcp-servers/bot/start.sh`. No routing and no secrets in the global
  config — `BOT_TOKEN` stays in `.env`, read by `start.sh`.
- Routing comes from the `agy` process environment (§3.1). Verified: stdio
  MCP servers are started as children of each `agy` invocation and inherit its
  environment.
- An `agy` run outside the bot starts the server without routing; the server's
  existing unbound-instance guard refuses to send ("запущен вне
  Telegram-сессии"). No change to `server.py` is expected.

### 3.7 Configuration and UI

- `topic_config.py` — `Engine` / `_VALID_ENGINES` and the validation sites
  accept `antigravity`.
- `/engine` keyboard — third button; unavailable engines are shown as such.
- Engine availability — `antigravity` is available only when
  `safe_antigravity_binary()` resolves. Selecting it on a host without `agy`
  gives a clear message instead of a failed spawn.
- Session storage — persist the engine alongside the session id; resolve the
  engine of a session from storage first and fall back to the UUID heuristic
  only for legacy entries.
- i18n strings for the new button, statuses and errors.

## 4. Error handling

| Situation | Behaviour |
|-----------|-----------|
| `agy` missing | Engine unavailable; `/engine` explains; existing topics unaffected. |
| Location / eligibility error | Error message that names the account as the cause. No automatic retry. |
| Not logged in | Error message pointing to the setup skill. |
| Transcript never appears after the first prompt | Same timeout path and user message as a Codex transcript that never appears. |
| Unknown event or step type | Ignored and logged at debug level. |
| Image path missing or outside the conversation's `brain` directory | Skipped with a warning; the text answer is unaffected. |
| `agy mcp add` fails at start-up | Warning in the log; the engine still works without bot MCP tools. |

## 5. Testing

- Unit tests on recorded fixtures (sanitised, no account data):
  - stream-json runs: plain answer, tool calls, `generate_image`, `ERROR`
    result, `AGY_ERROR` stderr line;
  - `transcript_full.jsonl`: multi-step turn with interim text, image step.
- Pane fixtures for `is_prompt_ready` / `is_modal_present`: idle, busy, trust
  dialog, scrollback that still contains an old dialog.
- Command builders, binary resolution, process environment (routing present,
  auto-update disabled).
- Session-engine storage: `agy` id resolves to `antigravity`, legacy ids keep
  the heuristic.
- MCP registration idempotency with a stubbed `agy`.
- `tests/test_public_runtime.py` — engine assertions extended.
- Live check on the development host: one tmux topic and one subprocess topic
  on `antigravity`, covering the success criteria in §1.

## 6. Delivery stages

Each stage ends green on `ruff`, `mypy`, `pytest`, and is a separate set of
commits.

1. **Core** — engine literal, validation, display name, `/engine` button,
   binary resolution, availability, explicit session-engine storage, i18n.
2. **tmux mode** — adapter TUI methods, readiness with trust auto-confirm,
   conversation discovery, transcript parser, cancel, recovery.
3. **subprocess mode** — stream-json parser, `_build_exec_command` branch.
4. **Bot MCP** — registration at start-up, routing environment.
5. **Images** — media extraction, subprocess lookup helper, de-duplication.
6. **Docs and rollout** — protocol reference, `project-knowledge` and
   `topic-setup` skills, live check, service restart.

## 7. Engine credentials — hard rules

`agy` keeps its own OAuth credential in a plain file, `~/.gemini/antigravity-cli/
antigravity-oauth-token` (mode 600). The bot never reads, forwards or stores it —
`agy` is simply exec'd and authenticates itself. Keep it that way.

**Never:**

- **Commit it, or any excerpt of it.** It is a bearer credential whose scopes
  include full cloud-platform access — treat it exactly like a password.
- **Print it.** No adapter may echo the token, the token file, or a process
  environment that could contain it into logs, Telegram messages, stream events
  or issue reports. Redact before pasting any `agy` log.
- **Copy the token file to a second machine.** Both copies then share one
  refresh token; the provider rotates it and one host silently loses auth.
  Authenticate each host separately instead — concurrent tokens for the same
  client and account coexist fine.
- **Sign the same account in from a second host or network.** Observed
  behaviour: authenticating an account from a network the provider does not
  accept made the *already working* host start failing with
  `FAILED_PRECONDITION (400): User location is not supported for the API use`.
  Removing the second host's token restored it, but only after a delay of
  several minutes. Give each host its own account if more than one needs the
  engine.
- **Put it in `.env`, `topic_config.json`, or any repository file.** Engine
  credentials live only in the CLI's own directory, which is runtime state and
  is not committed.

## 8. Getting started with `agy`

Install (no Node required):

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash   # → ~/.local/bin/agy
agy --version
```

Sign in. Headless print mode aborts after 60 s, so run the login inside the
TUI, which waits indefinitely:

```bash
tmux new -s agy-login
agy                       # choose "Google OAuth"
```

The CLI prints an authorization URL; the callback is a hosted page, so no
localhost port forwarding is needed on a headless host. Open the URL in any
browser, sign in, and paste the code back into the TUI. A browser account
verification step may follow — the CLI prints that link too. Onboarding then
asks for a theme and shows a Terms-of-Service screen whose data-collection
checkbox is opt-in.

Verify the engine end to end before enabling it for a topic:

```bash
agy -p "Reply with exactly: ok" --output-format json --dangerously-skip-permissions
```

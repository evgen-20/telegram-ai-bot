# Antigravity Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `engine: antigravity` a first-class engine (tmux and subprocess modes, bot MCP tools, automatic image delivery) backed by Antigravity CLI (`agy`).

**Architecture:** A new `services/antigravity.py` owns everything `agy`-specific (binary, env, argv, stream-json parser, transcript parser, conversation discovery, image lookup). Existing session paths gain thin branches: `claude.py` for subprocess, `tmux_manager.py` / `tail_runner.py` / `tmux_state.py` / `tmux_recovery.py` for tmux, reviving the adapter-TUI shape the legacy Codex TUI path used. The dispatcher routes `antigravity` to `tmux_manager`.

**Tech Stack:** Python 3.12, aiogram 3, asyncio subprocesses, tmux, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-27-antigravity-engine-design.md`; protocol facts in `docs/antigravity-protocol/README.md`.

## Global Constraints

- Engine literal value is exactly `"antigravity"`; display name `Antigravity`.
- The bot never reads, logs or copies `~/.gemini/antigravity-cli/antigravity-oauth-token`.
- `agy` is launched with `--dangerously-skip-permissions` and env `AGY_CLI_DISABLE_AUTO_UPDATE=1`.
- Routing env for the bot MCP server: `TELEGRAM_CHAT_ID`, `TELEGRAM_THREAD_ID` (empty string when none), `TELEGRAM_CONTEXT_LOCK=1`.
- `antigravity` is never an automatic fallback target, and a missing `agy` never silently falls back to another engine.
- tmux `runner_version` for this engine: `antigravity-tui-v1`.
- Public repository: no host names, IPs, account names or personal paths in code, tests, fixtures or docs.
- Checks before each commit: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src/ mcp-servers/bot/server.py`, `env -u TELEGRAM_BOT_TOKEN -u DEEPGRAM_API_KEY uv run pytest`.

## Review Focus

- **Concurrent conversations** — two `agy` topics (or a manual `agy` run) start at the same time; each topic must bind to its own `brain/<id>` directory. Pinned in Task 6 (`locate_conversation` matches the prompt text, ignores pre-existing and foreign directories).
- **Cancelled turn** — Esc leaves a `USER_INPUT` with no closing `PLANNER_RESPONSE`; the next `USER_INPUT` must close the dangling turn so the channel is not stuck processing. Pinned in Task 5.
- **Background-task continuation** — a `SYSTEM_MESSAGE` after a closed turn followed by a `PLANNER_RESPONSE` (no user input) must be delivered as its own turn, not dropped. Pinned in Task 5.
- **Missing `agy` on a host** — a topic configured for `antigravity` gets an explicit "Antigravity CLI not found" answer in both modes, with no engine switch. Pinned in Task 2.
- **Image path outside the conversation directory** — a crafted `media.uri` pointing elsewhere (e.g. `/etc/passwd`) must not be sent to Telegram. Pinned in Task 9.

---

## Stage 1 — Core

### Task 1: `services/antigravity.py` foundation

**Files:**
- Create: `src/telegram_bot/core/services/antigravity.py`
- Test: `tests/test_antigravity.py`

**Interfaces:**
- Produces:
  - `AGY_HOME: Path` = `~/.gemini/antigravity-cli`
  - `antigravity_binary() -> str` (raises `RuntimeError` on unsafe configured path)
  - `safe_antigravity_binary() -> str | None`
  - `antigravity_process_env(channel_key: ChannelKey | None, *, binary: str | None = None, base_env: dict[str, str] | None = None) -> dict[str, str]`
  - `brain_dir(conversation_id: str, *, home: Path | None = None) -> Path`
  - `transcript_path(conversation_id: str, *, home: Path | None = None) -> Path` → `brain_dir/.system_generated/logs/transcript_full.jsonl`
  - `is_conversation_id(value: str) -> bool` (UUID shape)
  - `build_exec_argv(prompt: str, *, conversation_id: str | None, model: str | None, binary: str | None = None) -> list[str]`
  - `build_tui_argv(*, conversation_id: str | None, model: str | None, binary: str | None = None) -> list[str]`
  - `build_tui_command(channel_key: ChannelKey, *, conversation_id: str | None, model: str | None) -> list[str]` — `["env", "-i", *KEY=VAL, *build_tui_argv(...)]` so `tmux_manager` uses it verbatim.

Binary resolution mirrors `providers.claude_binary()`: `TELEGRAM_AGY_BIN` override (absolute, safe-owned, else `RuntimeError`), then `~/.local/bin/agy`, then `shutil.which("agy")`; safety via `providers._is_safe_owned_executable`. Env = `providers.agent_process_env(binary=...)` + routing + `AGY_CLI_DISABLE_AUTO_UPDATE=1`.

Exec argv: `[bin, "-p", prompt, "--output-format", "stream-json", "--dangerously-skip-permissions", *(["--conversation", id] if id), *(["--model", m] if m)]`. The prompt goes after `-p` as one argv element (a prompt beginning with `-` is still one value because `-p` consumes the next argument — covered by a test that asserts position, not by shell quoting).
TUI argv: `[bin, "--dangerously-skip-permissions", *conversation, *model]`.

- [ ] Step 1: tests — binary override accepted/rejected, fallback path order, env contains routing + auto-update flag and no `TELEGRAM_BOT_TOKEN` even if present in `base_env`, thread id `None` → `""`, exec/TUI argv with and without conversation/model, `build_tui_command` starts with `env -i` and contains the routing vars, `transcript_path` layout, `is_conversation_id`.
- [ ] Step 2: run, expect import failure.
- [ ] Step 3: implement.
- [ ] Step 4: run tests + ruff + mypy.
- [ ] Step 5: commit `feat(antigravity): add agy binary, env and argv helpers`.

### Task 2: Engine literal, availability, picker, i18n

**Files:**
- Modify: `src/telegram_bot/core/services/providers.py` (`Engine`, `engine_display_name`, `is_engine_available`, `choose_available_engine`)
- Modify: `src/telegram_bot/core/services/topic_config.py:41-42`
- Modify: `src/telegram_bot/core/services/resume_listing.py:14` (`EngineName`)
- Modify: `src/telegram_bot/core/services/session_backend.py` (`BackendDispatcher` accepts optional `antigravity` backend)
- Modify: `src/telegram_bot/__main__.py:233` (route `antigravity` → `tmux_manager`)
- Modify: `src/telegram_bot/core/keyboards.py:96-115` (third button)
- Modify: `src/telegram_bot/core/handlers/commands.py:946` (map raw value to engine)
- Modify: `src/telegram_bot/core/handlers/text.py:109` (accept `antigravity` reply refs)
- Modify: `src/telegram_bot/core/messages.py` (en + ru: `ui.antigravity_not_found`)
- Modify: `src/telegram_bot/core/services/claude.py` (`send_stream`), `src/telegram_bot/core/handlers/streaming.py:383` (`ensure_exec_mode_ready`)
- Test: `tests/test_antigravity_engine.py`, extend `tests/test_public_runtime.py`

**Interfaces:**
- Produces: `Engine = Literal["claude", "codex", "antigravity"]` in both `providers.py` and `topic_config.py`; `engine_display_name("antigravity") == "Antigravity"`; `is_engine_available("antigravity")` ⇔ `safe_antigravity_binary() is not None`; `choose_available_engine("antigravity")` returns `"antigravity"` or `None` (never another engine); `engine_keyboard` emits callback `engine:antigravity`.
- `ui.agent_cli_not_found` stays for claude/codex; when the requested engine is `antigravity` and it is missing, both `send_stream` and `ensure_exec_mode_ready` answer `ui.antigravity_not_found`.

- [ ] Step 1: tests — topic config accepts/persists `antigravity`; `choose_available_engine` with monkeypatched availability (antigravity missing → `None`; claude missing → codex unchanged); keyboard has three buttons with the check mark on the current one; `engine_display_name`; dispatcher returns the tmux backend for `antigravity`.
- [ ] Step 2: run, expect failures.
- [ ] Step 3: implement.
- [ ] Step 4: full check suite.
- [ ] Step 5: commit `feat(engine): accept antigravity as a topic engine`.

## Stage 2 — subprocess mode

(Built before tmux because it exercises the adapter end to end with the smallest surface; the spec's stage order is about delivery, not dependency.)

### Task 3: stream-json parser

**Files:**
- Modify: `src/telegram_bot/core/services/antigravity.py` (`AntigravityExecParser`)
- Modify: `src/telegram_bot/core/services/providers.py` (`AntigravityAdapter.parse_exec_event` delegates; `ANTIGRAVITY_ADAPTER`)
- Create: `tests/fixtures/antigravity/exec_*.ndjson`
- Test: `tests/test_antigravity_exec.py`

**Interfaces:**
- Produces: `class AntigravityExecParser` with `parse(line: str) -> ExecParseResult` (stateful: remembers `conversation_id`, streamed text, pending `generate_image` steps) and `pending_images() -> list[int]` (step indexes of finished `generate_image` calls, drained on read).

Mapping (from the protocol reference):
- `init` → `session_id = conversation_id`.
- `step_update` `agent_response` with `text_delta` → `StreamEvent("text", delta)` is NOT emitted per delta (Telegram text events are whole blocks); deltas are accumulated per `step_index` and flushed as one `text` event when that step reaches `DONE`, except the last response step, which becomes the result.
- `step_update` `tool` `ACTIVE` → `StreamEvent("status", _tool_status_line(tool_name, parameters))`; `generate_image` → `"🎨 Generating image…"`.
- `step_update` `tool` `DONE` with `tool_name == "generate_image"` → record step index for image lookup.
- `result` `SUCCESS` → `StreamEvent("result", response)`; `ERROR` → `StreamEvent("result", <friendly error>)`, where location/eligibility errors map to `ui.antigravity_location_error`, auth errors to `ui.antigravity_auth_error`.
- Non-JSON line starting with `AGY_ERROR:` → parsed like an `ERROR` result if no result arrives; other non-JSON → ignored.
- Unknown events/step types → ignored.

Fixtures are sanitised copies of real runs: plain answer, two tools, `generate_image`, location `ERROR`.

- [ ] Step 1: fixtures + tests per mapping bullet.
- [ ] Step 2: run, fail.
- [ ] Step 3: implement.
- [ ] Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): parse agy stream-json events`.

### Task 4: subprocess execution branch

**Files:**
- Modify: `src/telegram_bot/core/services/claude.py` (`_build_exec_command` ~L458, `_run_cc_stream` loop ~L1026, stderr capture)
- Test: `tests/test_antigravity_exec.py` (extend)

**Interfaces:**
- Consumes: `build_exec_argv`, `antigravity_process_env`, `AntigravityExecParser`.
- `_build_exec_command` for `session.engine == "antigravity"` returns `ExecCommand(argv=build_exec_argv(full_prompt, conversation_id=session.session_id, model=session.model), cwd=cwd, env=antigravity_process_env((chat_id, thread_id)))`, where `full_prompt` comes from `_build_full_prompt` (mode prompt + Telegram context on a new conversation only). stdin is `DEVNULL`.
- Stream loop: `provider == "antigravity"` → per-run `AntigravityExecParser`; any parsed line resets the inactivity timer.

- [ ] Step 1: tests — `_build_exec_command` argv/env for new and resumed sessions; a fake process emitting a fixture drives `_run_cc_stream` to the right events, result and session id.
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): run agy in subprocess mode`.

## Stage 3 — tmux mode

### Task 5: transcript parser

**Files:**
- Modify: `src/telegram_bot/core/services/antigravity.py` (`AntigravityTranscriptParser`)
- Create: `tests/fixtures/antigravity/transcript_*.jsonl`
- Test: `tests/test_antigravity_transcript.py`

**Interfaces:**
- Produces: `class AntigravityTranscriptParser` with `parse(raw: str) -> TuiParseResult` and `current_turn_id: str | None`. Turn ids are `f"agy-{step_index}"` of the opening step.

Rules:
- `USER_INPUT` → if a turn is open, `turn_end(open)`; then `turn_start(agy-<step>)`.
- `SYSTEM_MESSAGE` → if no turn is open, `turn_start(agy-<step>)` (background-task continuation); if a turn is open, nothing.
- `PLANNER_RESPONSE`:
  - non-empty `content` and `tool_calls` present → `text(content)`;
  - each `tool_calls[i]` → `status(_tool_status_line(name, args))`;
  - no `tool_calls` → `result_message(content)` (if non-empty) then `turn_end(current)`; turn closed.
  - When no turn is open (e.g. tail attached mid-conversation), open one implicitly first.
- `GENERIC` → for each `media[]` with `mime_type` starting `image/` → `image_message(path)` where path = `uri` with `file://` stripped; the path must resolve inside the conversation's `brain_dir` (parser is constructed with `brain_root: Path | None`), otherwise dropped with a warning.
- Any other type or malformed JSON → no events.
- All events carry `turn_id`.

- [ ] Step 1: fixtures (multi-step turn, cancelled turn followed by a new input, background continuation, image step, foreign image path) + tests.
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): parse agy conversation transcripts`.

### Task 6: TUI adapter surface and conversation discovery

**Files:**
- Modify: `src/telegram_bot/core/services/antigravity.py`
- Modify: `src/telegram_bot/core/services/providers.py` (`AntigravityAdapter` TUI methods)
- Test: `tests/test_antigravity_tui.py`

**Interfaces:**
- Produces:
  - `is_prompt_ready(pane: str) -> bool` — live tail (last 6 non-blank lines) contains `? for shortcuts` and not `esc to cancel`, and no trust dialog.
  - `is_trust_dialog(pane: str) -> bool` — `"Do you trust the contents of this project?"` and `"Yes, I trust this folder"` in the live tail (last 12 non-blank lines).
  - `is_modal_present(pane: str) -> bool` — `↑/↓ Navigate` in the live tail.
  - `snapshot_conversations(*, home: Path | None = None) -> frozenset[str]` — names of existing `brain/` dirs.
  - `locate_conversation(snapshot: frozenset[str], prompt: str, *, home: Path | None = None, timeout_sec: float = 30.0) -> str | None` (async) — polls for a new dir whose `transcript_full.jsonl` first `USER_INPUT` content contains the first 200 chars of `prompt` (whitespace-normalised); returns its id.
  - `transcript_has_user_input(path: Path, offset: int, prompt: str) -> bool` — delivery ack for an existing conversation.

- [ ] Step 1: pane fixtures (idle, busy, trust, scrollback with an old dialog above an idle footer) and discovery tests with a temp `home` (pre-existing dir ignored, foreign new dir with another prompt ignored, matching dir returned, timeout → `None`).
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): detect agy TUI state and new conversations`.

### Task 7: tmux integration

**Files:**
- Modify: `src/telegram_bot/core/services/tmux_manager.py` (start/readiness/send/discovery/switch/clear/transcript path/session-id shape/modal detector/runner_version)
- Modify: `src/telegram_bot/core/services/tail_runner.py:130` (parser selection) and `_process_lines` (antigravity parser path)
- Modify: `src/telegram_bot/core/services/tmux_state.py:451-475` (`peek_saved_session`)
- Modify: `src/telegram_bot/core/services/tmux_recovery.py:212-225` (restore antigravity sessions)
- Test: `tests/test_antigravity_tmux.py`

Behaviour:
- `start_session(provider="antigravity")`: `startup_cmd = build_tui_command(channel_key, conversation_id=resume_id, model=model)`; `transcript_path` stored when resuming; `runner_version="antigravity-tui-v1"`.
- Readiness: `_await_antigravity_prompt_ready(name, timeout)` — press Enter once on the trust dialog, then wait for `is_prompt_ready`; kill the session on timeout (same as Codex).
- Send: `_safe_send_antigravity` — refuse on modal (alert as Codex does), paste with `send_text_to_tmux(..., submit_enter=False)`, press Enter, then confirm delivery: for a known conversation via `transcript_has_user_input` within 10 s; for a new one via `locate_conversation` (sets `state.session_id`, `state.transcript_path`, `state.offset=0`, saves state). Failure → alert + `""`.
- Tail: `TailRunner` picks `AntigravityTranscriptParser(brain_root=brain_dir(session_id))` for `provider == "antigravity"`; `_process_lines` treats it like the Codex parser branch (`parsed.events`, `parsed.session_id`).
- Modal detector, `_transcript_path_for_state`, `_validate_session_id_shape`, `switch_session`, `clear_context` and recycle branches use the antigravity helpers.
- Recovery: `restore_all` accepts `provider == "antigravity"` with `antigravity-tui-v1`; `peek_saved_session` resolves `transcript_path(session_id)`.

- [ ] Step 1: tests with the existing tmux fakes (see `tests/test_tmux_manager_protocol.py`): startup argv, trust auto-confirm, send → discovery → state update, tail parser selection, state round-trip, restore acceptance.
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: live smoke in a scratch tmux server (manual script, not committed): start → prompt → events → cancel → resume.
- [ ] Step 6: commit `feat(antigravity): run agy in tmux topics`.

## Stage 4 — Bot MCP

### Task 8: register the bot server with `agy`

**Files:**
- Modify: `src/telegram_bot/core/services/antigravity.py` (`ensure_bot_mcp_registered`)
- Modify: `src/telegram_bot/__main__.py` (call at start-up when `agy` is available, off the event loop)
- Test: `tests/test_antigravity_mcp.py`

**Interfaces:**
- Produces: `ensure_bot_mcp_registered(app_root: Path, *, binary: str | None = None, run=subprocess.run) -> bool` — runs `agy mcp list`; if no line starts with `bot`, runs `agy mcp add -e APP_ROOT=<root> -e ENV_FILE=<root>/.env -e PROJECT_DIR=<root> bot bash <root>/mcp-servers/bot/start.sh`; returns success; never raises (warns).
- Routing is already in `antigravity_process_env` (Task 1).

- [ ] Step 1: tests with a stubbed `run`: already registered → no add; missing → exact add argv; failure → `False` + no exception.
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): expose the bot MCP server to agy`.

## Stage 5 — Images

### Task 9: automatic image delivery

**Files:**
- Modify: `src/telegram_bot/core/services/antigravity.py` (`find_generated_images`)
- Modify: `src/telegram_bot/core/services/claude.py` (subprocess: after a `generate_image` `DONE`, emit `image_message` for new images)
- Modify: `src/telegram_bot/core/handlers/streaming.py` (per-turn de-duplication of image paths in `dispatch_image_event` callers)
- Test: `tests/test_antigravity_images.py`

**Interfaces:**
- Produces: `find_generated_images(conversation_id: str, *, after_step: int = -1, home: Path | None = None) -> list[tuple[int, Path]]` — `(step_index, path)` for `GENERIC` steps with image media, path confined to `brain_dir(conversation_id)`.

- [ ] Step 1: tests — images found after a step, foreign path dropped, missing transcript → `[]`, subprocess run with a `generate_image` fixture yields one `image_message`, the same path twice in a turn is delivered once.
- [ ] Step 2: fail. Step 3: implement. Step 4: checks.
- [ ] Step 5: commit `feat(antigravity): deliver generated images to the chat`.

## Stage 6 — Docs and rollout

### Task 10: documentation, live verification, deploy

**Files:**
- Modify: `docs/antigravity-protocol/README.md` (anything learned during implementation)
- Modify: `.claude/skills/project-knowledge/SKILL.md` (and its references if engines are listed there), `.claude/skills/topic-setup/SKILL.md`, `.claude/skills/bot-setup/SKILL.md` (install/login pointer), `topic_config.example.json` if it enumerates engines, `README*` engine lists
- Modify: spec status line → `implemented`

- [ ] Step 1: docs.
- [ ] Step 2: full check suite.
- [ ] Step 3: restart `telegram-bot.service`, confirm clean start in the journal.
- [ ] Step 4: live check in a dedicated test topic: tmux (multi-step request, image, cancel, bot restart mid-turn, MCP `send_message`), subprocess (text + image).
- [ ] Step 5: commit docs, push to `fork`.

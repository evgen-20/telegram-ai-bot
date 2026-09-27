# Architecture

The bot runtime is split into:

- `src/telegram_bot/__main__.py` - public entry point, aiogram wiring, shutdown.
- `src/telegram_bot/core/handlers/` - Telegram command, text, media, voice, forward, topic, and TUI handlers.
- `src/telegram_bot/core/services/` - session management, provider adapters, topic config, streaming, tmux, resume, MCP runtime, transcription, incoming rich content normalization, and rich final-answer rendering.
- `src/telegram_bot/core/tui/` - tmux TUI capture, modal detection, keyboard controls, routing, and transcript helpers.
- `mcp-servers/bot/` - MCP server that lets an agent send messages, images,
  image galleries, and files back to Telegram.
- `src/telegram_bot/prompts/` - generic public prompt modes.

The reusable core registers only the public `free` and `task` prompt modes.
An application that embeds the core must register any additional prompt mode
and its complete tool policy outside `src/telegram_bot/core/`; adding a prompt
file alone does not make a mode runnable.

Two independent runtime axes are important:

- `engine`: `claude`, `codex`, or `antigravity`.
- `exec_mode`: `subprocess` or `tmux`.

Engine selection is availability-aware: Claude Code is preferred by default,
Codex is used when Claude Code is missing and Codex exists, and the bot remains
online with a user-facing install message when neither CLI is available.
Antigravity is opt-in only: it never takes part in the automatic fallback, and
a topic that selects it without an installed `agy` gets "Antigravity CLI not
found" instead of an engine switch.

`stream_mode` controls Telegram progress delivery:

- `verbose`: every tool/status event is a separate silent message.
- `live`: tool/status events share an editable progress buffer.
- `minimal`: tool/status events are suppressed.

Human-readable intermediate text remains separate in every stream mode. Provider
transcripts are normalized into turn start, ordered status/text events, one
logical final answer, and turn end. A live progress buffer belongs to one turn,
so late events cannot close a newer turn's progress surface.

Tmux transcript delivery uses a FIFO callback queue and persists a transcript
checkpoint only after all Telegram callbacks through that point complete.
Recovery rebuilds parser state from the nearest provider turn boundary before
resuming delivery. An uncertain or incomplete Telegram send may be replayed
after restart, but its missing suffix is not silently skipped.

Incoming Telegram rich messages are normalized into agent-readable text. Text
blocks, tables, footnotes, and structural placeholders stay visible; rich photo
blocks become image attachments when Telegram provides files. Outgoing rich
message rendering is intentionally narrow: intermediate progress always stays
plain, and only final answers with Markdown tables are eligible for Telegram
RichText/RichMessage delivery. Unsupported rich payloads fall back to plain
Telegram text. Telegram's schema starts at
https://core.telegram.org/type/RichText.

Forum topics are isolated by `(chat_id, thread_id)`. Session mappings and tmux
state are runtime files and must not be committed.

The public entrypoint uses a workspace-local, mode-0700 tmux server directory
when `TMUX_TMPDIR` is not configured. On the first upgraded start it migrates
only state-owned bot sessions from the legacy default server, leaving unrelated
tmux sessions untouched.

The standard installation uses one `PROJECT_ROOT`. Advanced installations may
split immutable application code (`APP_ROOT`) from editable projects and
runtime state (`AGENT_WORKSPACE_ROOT`). Relative topic config, session mapping,
tmux, cache, generated runtime MCP configs, and default-cwd paths resolve under
the workspace root. MCP launchers and optional base MCP profiles resolve under
the application root.

Long-lived tmux runtimes generate topic-scoped MCP runtime configs. These
configs tag child processes with non-secret runtime metadata so `/kill`,
`/recycle`, subprocess cleanup, and diagnostics can identify bot-owned MCP
processes without relying on private paths.

Operational commands include `/recycle` for restarting a stuck tmux runtime
while preserving resumable context when possible, and `/mcpstatus` for redacted
MCP process diagnostics in the current topic.

## Codex backend: app-server, not tmux (fork divergence)

Upstream drives Codex the same way it drives Claude Code — a CLI in a tmux
pane, scraped through the TUI, or a one-shot subprocess. This fork keeps that
code (it is still the Claude path, and the Codex subprocess path is still
reachable for `exec_mode: subprocess`), but a topic with `engine: codex` and
`exec_mode: tmux` is served by a Codex **app-server** instead:

- `core/services/codex_app_server.py` - JSON-RPC client over a
  `codex app-server --listen stdio://` subprocess; auto-answers approval
  requests.
- `core/services/codex_daemon.py` - best-effort start of the singleton
  `codex app-server daemon`; the bot works without it.
- `core/services/codex_events.py` - maps Codex notifications onto the same
  `StreamEvent` stream the Claude path emits, so stream modes, live buffers,
  and final-answer rendering are shared.
- `core/services/codex_session_manager.py` - per-channel thread lifecycle
  (start/resume/cancel/clear/restore), image snapshotting, wedged-client
  recycling.
- `core/services/session_backend.py` - the `SessionBackend` Protocol both
  `TmuxManager` and `CodexSessionManager` satisfy, plus `BackendDispatcher`.

Handlers never talk to `TmuxManager` for per-channel session state directly;
they resolve a backend through `BackendDispatcher.for_engine(topic.engine)`.
`TmuxManager` is still passed alongside it for infrastructure that has no
Codex equivalent (live buffers, session snapshots, topic config).

Consequences to keep in mind:

- Codex state lives under the `codex_sessions` top-level key of the same
  `state.json` the tmux backend writes. `StateStore.save()` merges into the
  on-disk dict so the key survives; readers that treat every top-level key as
  a channel key must skip `_FOREIGN_STATE_KEYS`.
- Images go to Codex as `localImage` `UserInput` entries (`attachments=` on
  `send_stream`), not as file paths in the prompt body. The Claude path accepts
  and ignores the kwarg.
- `TmuxManager.has_live_provider("codex")` is always false here, so anything
  guarding on "is a Codex session live" must also ask
  `CodexSessionManager.has_live_sessions()` - that is what the Codex CLI
  auto-updater does, via `wire_codex_liveness_probe`.
- `/recycle` and `/mcpstatus` remain tmux-only and answer "not active" in a
  Codex topic; `/new`, `/clear`, `/kill`, `/cancel`, and `/resume` are
  backend-routed and work for both.

Protocol schemas vendored from the Codex CLI live in `docs/codex-protocol/`.

## Antigravity backend (`agy`)

Antigravity CLI is the third engine. It reuses the Claude-style transports
rather than a separate backend: `BackendDispatcher` maps `antigravity` to the
same backend as `claude`.

- `core/services/antigravity.py` - owns everything `agy`-specific: binary
  discovery (`TELEGRAM_AGY_BIN`, then `~/.local/bin/agy`, then `PATH`, with an
  ownership/permission check), process environment (topic routing via
  `TELEGRAM_CHAT_ID`/`TELEGRAM_THREAD_ID`), exec and TUI argv, the stream-json
  parser `AntigravityExecParser`, the transcript parser
  `AntigravityTranscriptParser`, pane detection (trust dialog, modal, prompt
  ready), conversation discovery, `generate_image` output lookup, the
  `agy models` list for `/model`, and bot MCP registration.
- `core/services/claude.py` - the subprocess branch: runs
  `agy -p ... --output-format stream-json`, feeds stdout to
  `AntigravityExecParser`, and maps errors to friendly messages (not signed
  in, "User location is not supported" as an account/region issue).
- `core/services/tmux_manager.py` - the tmux branch: starts the `agy` TUI,
  auto-accepts the "trust this folder" dialog for the operator-chosen cwd,
  tails `~/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/logs/transcript_full.jsonl`
  through `AntigravityTranscriptParser`, `/cancel` sends Esc, `/clear` starts a
  fresh conversation, and `/recycle` restarts on the same one. Runner tag
  `antigravity-tui-v1` lets sessions survive a bot restart.

Notes:

- `agy` has no system-prompt flag, so the prompt mode and Telegram context go
  with the first message of a new conversation.
- A new tmux conversation's id is discovered by snapshotting existing
  conversations before the send and matching the new transcript's user input
  against the prompt text.
- `agy` has no per-run MCP flag. At start-up the bot registers a `bot` MCP
  server in `agy`'s global MCP config (`agy mcp add ... bot bash
  <repo>/mcp-servers/bot/start.sh`) unless a `bot` entry already exists; no
  secrets are written there, routing comes from each process environment.
- Only images produced by the built-in `generate_image` tool are forwarded to
  Telegram, never images the agent merely viewed.
- `/model` stores `models.antigravity` and continues the same conversation:
  subprocess on the next message, tmux by recycling the live pane.
- The bot never reads `agy`'s OAuth token under `~/.gemini/antigravity-cli/`.

Protocol reference: `docs/antigravity-protocol/README.md`. Design:
`docs/superpowers/specs/2026-09-27-antigravity-engine-design.md`.

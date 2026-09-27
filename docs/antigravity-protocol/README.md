# Antigravity CLI (`agy`) integration reference

Reference material for adding Antigravity CLI as a third engine next to Claude
Code and Codex. Everything below was captured from a live `agy` 1.2.12 install,
not from the vendor docs — the published docs describe a different event set
(`message` / `tool_use` / `tool_result`) that the CLI does not emit.

Refresh this file when bumping `agy`:

```bash
agy --version
agy models
agy -p "ping" --output-format stream-json --dangerously-skip-permissions
```

## Binary

`agy` is a single static Go binary (~220 MB) installed to `~/.local/bin/agy`.
No Node runtime is involved, so no shim wrapper is needed — unlike Gemini CLI,
it can be exec'd directly from a service process.

It **self-updates in the background**. A working setup can therefore change
version under you; `AGY_CLI_DISABLE_AUTO_UPDATE` pins it.

## Flags that matter for an adapter

| Flag | Purpose |
|------|---------|
| `-p` / `--print` | One-shot non-interactive turn |
| `--output-format` | `text`, `json`, `stream-json` |
| `--input-format stream-json` | Reads one NDJSON message per line from stdin, one turn each — keeps a single conversation open. Requires `--output-format stream-json` |
| `--conversation <id>` | Resume a specific conversation |
| `-c` / `--continue` | Continue the most recent conversation |
| `--model` | See `agy models` |
| `--effort` | `low` / `medium` / `high` |
| `--mode` | `accept-edits`, `plan` |
| `--dangerously-skip-permissions` | Auto-approve tool permissions (analogue of the Claude bypass mode) |
| `--add-dir` | Extra workspace directory |
| `--json-schema` | Constrain the final structured result |
| `--print-timeout` | `0` waits for the turn to complete |

Subcommands: `models`, `mcp` (add/remove/list/enable/disable), `agents`,
`plugin`, `remote-control`, `update`, `install`, `changelog`.

`agy` honours `HTTPS_PROXY` / `HTTP_PROXY`.

## `--output-format json`

A single object on stdout:

```json
{
  "conversation_id": "<uuid>",
  "status": "SUCCESS",
  "response": "ok\n",
  "duration_seconds": 1.77,
  "num_turns": 1,
  "usage": {
    "input_tokens": 12387, "output_tokens": 82, "thinking_tokens": 81,
    "cache_read_tokens": 0, "total_tokens": 12469
  }
}
```

Failures keep the same envelope with `"status": "ERROR"` and an `error` string,
and print an `AGY_ERROR: {...}` line to stderr carrying `status`, `error_code`,
`retryable` and an `error_id`.

## `--output-format stream-json`

Newline-delimited JSON. Three event kinds, each keyed by `event`:

```json
{"event":"init","conversation_id":"<uuid>","init":{"cwd":"...","tools":["generate_image","run_command","..."],"permission_mode":"always-proceed"}}
{"event":"step_update","step_update":{"conversation_id":"<uuid>","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"conversation_id":"<uuid>","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"Привет"}}
{"event":"step_update","step_update":{"conversation_id":"<uuid>","step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"\n","duration_seconds":1.65,"usage":{...}}}
{"event":"result","result":{"conversation_id":"<uuid>","status":"SUCCESS","response":"Привет\n","duration_seconds":1.73,"num_turns":1,"usage":{...}}}
```

Notes for the parser:

- `conversation_id` arrives on `init` — capture it there for resume.
- Assistant text streams as `text_delta` on `step_update` with
  `step_type: agent_response`; `state` walks `ACTIVE` → `DONE`.
- `usage` is attached to the terminating `step_update` and again to `result`.
- `result.response` holds the full answer, so a minimal adapter can ignore
  deltas and still deliver a final message.
- Tool activity is `step_type: tool` with `tool_name` and `tool_info`:

  ```json
  {"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"cat a.txt"}}}}
  {"event":"step_update","step_update":{"step_index":2,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.03,"tool_info":{"name":"run_command","parameters":{"CommandLine":"cat a.txt"},"output":"hello\r\n"}}}
  ```

  Parameter keys are tool-specific (`CommandLine`, `AbsolutePath`,
  `ImageName` + `Prompt` for `generate_image`). `output` appears only on
  `DONE` and not for every tool.
- `generate_image` `DONE` carries **no file path**. Take it from the
  conversation transcript (see below).
- `agent_response` steps between tool calls may be `DONE` without any
  `text_delta`.

## Resume

`--conversation <id>` works for both print mode and the TUI, from any `cwd`;
the conversation keeps its history and the transcript keeps growing in the
same file.

## Conversation transcript

Every conversation has a directory named after its `conversation_id`:

```
~/.gemini/antigravity-cli/brain/<conversation_id>/
  <ImageName>_<epoch_ms>.jpg              # generate_image output
  .system_generated/logs/transcript.jsonl
  .system_generated/logs/transcript_full.jsonl
  .system_generated/steps/<n>/output.txt
```

`transcript_full.jsonl` is append-only, one step per line, written **while
the turn runs** (in the TUI too), so it can be tailed like the Claude and Codex
JSONL transcripts. `transcript.jsonl` is the same with JSON-quoted tool
arguments and truncated long fields; prefer the `_full` variant.

```json
{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"…","content":"<USER_REQUEST>\n…"}
{"step_index":1,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"…","content":"","thinking":"…","tool_calls":[{"name":"run_command","args":{"CommandLine":"sleep 4","Cwd":"…"}}]}
{"step_index":2,"source":"MODEL","type":"GENERIC","status":"DONE","created_at":"…","content":"…The command exited with code 0…"}
{"step_index":3,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"…","content":"Interim text","tool_calls":[…]}
{"step_index":7,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","created_at":"…","content":"Final answer"}
```

- `PLANNER_RESPONSE.content` is assistant text (interim or final);
  `tool_calls` lists the calls it made. A `PLANNER_RESPONSE` without
  `tool_calls` ends the turn.
- `GENERIC` is a tool result. After `generate_image` it carries
  `media: [{"mime_type":"image/jpeg","uri":"file:///…/brain/<id>/<name>.jpg"}]`;
  a `view_file` on an image carries the same shape with a bare path.

## MCP servers

`agy mcp add|remove|list|enable|disable` edits a single global file,
`~/.gemini/config/mcp_config.json`; there is no per-invocation config flag.
Stdio servers are started as children of each `agy` process and inherit its
environment, so per-run context (for example the Telegram chat to route to)
can be passed as environment variables of the `agy` process itself.

```bash
agy mcp add -e KEY=value <name> <command> [args...]   # flags before <name>
```

## Agent tools

`init.tools` on a default run includes `generate_image` (Imagen/nano-banana),
`search_web`, `read_url_content`, `run_command`, `write_to_file`,
`replace_file_content`, `grep_search`, `find_by_name`, `view_file`,
`notebook_edit`, a full `browser_*` set (DOM, clicks, console logs,
screenshots), `call_mcp_tool`, sub-agent management and `schedule`.

`call_mcp_tool` plus `agy mcp add` is the hook for this repository's own MCP
server, i.e. the path for sending generated images and files back to Telegram.

## Quota

There is no headless usage command. Quota is only visible inside the TUI via
`/usage` (alias `/quota`): two model groups (Gemini Flash+Pro; Claude+GPT-OSS),
each with a weekly and a rolling 5-hour limit, consumed proportionally to token
cost. Surfacing limits in the bot would require screen-scraping the TUI until
the vendor ships a flag.

## Interactive surface (for a tmux-mode adapter)

- First run in a directory shows a **trust dialog** ("Do you trust the contents
  of this project?", options "Yes, I trust this folder" / "No, exit", footer
  `↑/↓ Navigate · enter Confirm`). "Yes" is preselected, so a single Enter
  accepts it. It blocks the pane until answered.
- Idle footer: `? for shortcuts` under the `>` input line. While a turn runs
  the footer reads `esc to cancel` (Escape as the cancel key is taken from
  that hint, not yet exercised).
- The header shows the version, the signed-in account with its plan, the model
  and the workspace path.
- Onboarding adds a theme picker and a Terms-of-Service screen with a
  data-collection checkbox.
- The TUI login screen waits indefinitely for a pasted authorization code,
  whereas print mode gives up after 60 s — relevant if a setup flow is ever
  automated.

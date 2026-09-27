# Testing

Standard checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
uv run pytest
```

If the shell inherited the bot service environment, run pytest as
`env -u TELEGRAM_BOT_TOKEN -u DEEPGRAM_API_KEY uv run pytest`; otherwise the
dotenv loading test sees the inherited values.

Import smoke:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run python -c "import telegram_bot; import telegram_bot.__main__"
```

Minimal public tests should cover:

- config defaults;
- exact dotenv credential loading and split application/workspace roots;
- prompt fallback;
- topic config parsing, including per-engine model overrides;
- provider parser behavior for current Codex item events, clarification finals,
  Claude local-command transcript records, and Antigravity stream-json and
  transcript records (`tests/fixtures/antigravity/`);
- Antigravity tmux integration: tests drive a fake `agy` TUI
  (`tests/fixtures/antigravity/fake_agy.py`) on an isolated tmux server, never
  the real CLI or account;
- MCP bot server importability;
- no private default working directory;
- public command wiring for `/mode`, `/engine`, `/model`, `/stream`, `/tui`,
  `/resume`, `/recycle`, and `/mcpstatus`;
- public MCP tool allowlists, including `send_image_gallery` and Context7
  documentation tools.
- ordered-list fallback when Telegram rich rendering would restart numbering.

Manual Telegram QA is required before publication. Private deployed-bot QA is a
regression signal; public-checkout/staging QA validates the public artifact.

After local checks, remove generated artifacts before release staging:
`.venv/`, `.ruff_cache/`, `.mypy_cache/`, `.pytest_cache/`, `__pycache__/`,
`*.pyc`, and `*.pyo`.

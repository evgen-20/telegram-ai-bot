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

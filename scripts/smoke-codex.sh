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

    # Signature mirrors tests/test_codex_session_manager.py verbatim.
    # ``mode`` is a Literal (see cc_modes.Mode) — pass the string directly.
    await mgr.start_session(
        ch,
        mode="free",
        cwd="$WORKDIR",
        mcp_config="",
        chat_id=ch[0],
        session_manager=object(),
        resume_session_id=None,
        provider="codex",
        model=None,
    )

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

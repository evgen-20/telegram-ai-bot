"""Pane capture helpers: TUI state detectors, HTML escaping, readiness wait.

State detectors are substring-matching predicates on `tmux capture-pane -p`
output. `escape_pane_for_html` prepares a snapshot for Telegram `<pre>`
blocks — it HTML-escapes `&<>` and strips C0 control bytes (while keeping
`\\t` and `\\n`) to avoid Telegram rejecting the message.

`await_prompt_ready` is a net-new async polling wrapper used by the tmux
runner at session spawn. It auto-accepts the trust dialog (selecting the
affirmative option explicitly, see `trust_accept_keys`), then polls until the
prompt marker appears. On timeout it sends one fallback Enter, polls for
another 5s, then kills the session and returns False.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import subprocess
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Real CC 2.1.114 trust-dialog text observed in PoC on 2026-04-19.
TRUST_DIALOG_SUBSTRINGS = (
    "trust this folder",
    "Do you trust the contents of this directory",  # Codex / older CC, fallback
)

# Trust-dialog option lines. CC 2.1.259 renders them as
#
#     ❯ No, exit
#       Yes, I trust this folder
#
# i.e. the cursor starts on the DECLINE option — a blind Enter quits the CLI
# and the tmux window dies. We therefore locate the affirmative line and walk
# the cursor onto it before confirming. The optional `\d+[.)]` prefix covers
# the numbered variant codex uses ("1. Yes, continue").
_TRUST_OPTION_RE = re.compile(r"^\s*(?:❯\s*)?(?:\d+[.)]\s*)?(?:yes|no)\b", re.IGNORECASE)
_TRUST_ACCEPT_RE = re.compile(r"^\s*(?:❯\s*)?(?:\d+[.)]\s*)?yes\b", re.IGNORECASE)
_CURSOR_GLYPH = "❯"

# Prompt-ready markers in CC TUI:
#   "❯ " — idle prompt, ready for input
#   "start a new conversation" — welcome screen
#   "/help" — mentioned in the welcome shortcut list
READINESS_MARKERS = (
    "❯",
    "start a new conversation",
    "/help",
)

_C0_STRIP_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_PROMPT_LINE_RE = re.compile(r"(?m)^[❯>]\s")

_POLL_INTERVAL_SEC = 0.5
_FALLBACK_POLL_BUDGET_SEC = 5.0

# The TUI renders the trust dialog seconds before its input handler is wired
# up, so the first keystrokes are swallowed (same failure mode
# `_probe_input_ready` guards against). One attempt is not enough: retry every
# `TRUST_ACCEPT_RETRY_SEC` until the dialog disappears or the budget is spent.
TRUST_ACCEPT_RETRY_SEC = 2.0
TRUST_ACCEPT_MAX_ATTEMPTS = 6


def is_trust_dialog(pane_text: str) -> bool:
    return any(s in pane_text for s in TRUST_DIALOG_SUBSTRINGS)


def trust_accept_keys(pane_text: str) -> list[str] | None:
    """Keys that move the trust-dialog cursor onto "yes" and confirm it.

    Returns None when the pane does not expose both an affirmative option and
    a cursor — callers must then send nothing at all, because any blind Enter
    on this dialog lands on "No, exit" and kills the CLI.
    """
    options = [line for line in pane_text.splitlines() if _TRUST_OPTION_RE.match(line)]
    accept = next((i for i, line in enumerate(options) if _TRUST_ACCEPT_RE.match(line)), None)
    cursor = next((i for i, line in enumerate(options) if _CURSOR_GLYPH in line), None)
    if accept is None or cursor is None:
        return None
    step = "Down" if accept > cursor else "Up"
    return [step] * abs(accept - cursor) + ["Enter"]


def is_prompt_ready(pane_text: str) -> bool:
    # A trust dialog is never "ready": its decline option renders as "❯ No,
    # exit", which can satisfy the prompt-line regex on a narrow pane.
    if is_trust_dialog(pane_text):
        return False
    if _PROMPT_LINE_RE.search(pane_text):
        return True
    return any(m in pane_text for m in ("start a new conversation", "/help"))


def escape_pane_for_html(pane_text: str) -> str:
    """Prepare a raw `tmux capture-pane -p` snapshot for Telegram `<pre>`.

    HTML-escape `&<>` (quote=False keeps `'` and `"` as-is since they are
    safe inside `<pre>`), then strip C0 control bytes `[\\x00-\\x08\\x0b-\\x1f]`
    plus DEL `\\x7f` which Telegram rejects. `\\t` (0x09) and `\\n` (0x0a)
    are preserved.
    """
    escaped = html.escape(pane_text, quote=False)
    return _C0_STRIP_RE.sub("", escaped)


def _capture_pane(session_name: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", f"={session_name}:", "-p", "-S", "-200"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _send_enter(session_name: str) -> None:
    _send_keys(session_name, ["Enter"])


def _send_keys(session_name: str, keys: list[str]) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"={session_name}:", *keys],
        check=True,
    )


def _kill_session(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={session_name}"],
        check=False,
    )


async def _accept_trust(session_name: str, pane: str, *, attempt: int) -> bool:
    """Send the affirmative answer to a visible trust dialog. False if the
    pane layout was not recognised and nothing was sent."""
    keys = trust_accept_keys(pane)
    if keys is None:
        logger.warning(
            "TUI_IO: trust dialog on session=%s has no recognisable options; "
            "sending nothing (a blind Enter would decline). Pane:\n%s",
            session_name,
            "\n".join(pane.splitlines()[-10:]),
        )
        return False
    logger.info(
        "TUI_IO: auto-accepting trust dialog for session=%s (attempt %d, keys=%s)",
        session_name,
        attempt,
        keys,
    )
    await asyncio.to_thread(_send_keys, session_name, keys)
    return True


async def await_prompt_ready(
    session_name: str,
    timeout: float = 30.0,
    clock: Callable[[], float] | None = None,
) -> bool:
    """Poll a tmux session until the Claude CLI prompt is ready.

    Polls `tmux capture-pane` every 500ms. Exit conditions:
      (a) trust-dialog detected → send the keys that select its affirmative
          option (`trust_accept_keys`), keep polling. Retried up to
          `TRUST_ACCEPT_MAX_ATTEMPTS` times because the TUI swallows input
          for the first seconds after it paints the dialog.
      (b) prompt-ready detected → return True.
      (c) deadline reached → send one fallback Enter, poll for 5 more
          seconds; if still not ready → `tmux kill-session` + return False.
          The fallback Enter is suppressed while the trust dialog is up: it
          would land on "No, exit" and quit the CLI.

    Auto-accepting trust is deliberate. Every directory the runner spawns in
    comes from `topic_config.json`, i.e. the operator wired it to a topic by
    hand; re-asking in a pane nobody is watching only produces a spawn
    timeout.

    `clock` is injectable for tests (default `time.monotonic`). Allows
    sharing a deadline with `_spawn_tmux` (Wave 2, Decision 7 shared budget).
    Any `subprocess.CalledProcessError` from capture-pane means tmux died
    — return False immediately.

    Due to the 5s fallback window, the minimum wall-time to a False result
    is ~5s even if `timeout` is smaller.
    """
    clock = clock or time.monotonic
    deadline = clock() + timeout
    trust_attempts = 0
    last_trust_attempt = float("-inf")
    pane = ""  # captured in loop; initialized so the timeout log never UnboundLocalError.

    while clock() < deadline:
        try:
            pane = await asyncio.to_thread(_capture_pane, session_name)
        except subprocess.CalledProcessError:
            return False

        if is_trust_dialog(pane):
            now = clock()
            if (
                trust_attempts < TRUST_ACCEPT_MAX_ATTEMPTS
                and now - last_trust_attempt >= TRUST_ACCEPT_RETRY_SEC
            ):
                last_trust_attempt = now
                trust_attempts += 1
                if await _accept_trust(session_name, pane, attempt=trust_attempts):
                    await asyncio.sleep(_POLL_INTERVAL_SEC)
                    continue
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            continue

        if is_prompt_ready(pane):
            return True

        await asyncio.sleep(_POLL_INTERVAL_SEC)

    # Main loop exhausted — one fallback Enter, then 5s grace window. With the
    # trust dialog still up, Enter means "No, exit"; retry the accept instead.
    try:
        if is_trust_dialog(pane):
            await _accept_trust(session_name, pane, attempt=trust_attempts + 1)
        else:
            await asyncio.to_thread(_send_enter, session_name)
    except subprocess.CalledProcessError:
        return False

    fallback_deadline = clock() + _FALLBACK_POLL_BUDGET_SEC
    while clock() < fallback_deadline:
        try:
            pane = await asyncio.to_thread(_capture_pane, session_name)
        except subprocess.CalledProcessError:
            return False
        if is_prompt_ready(pane):
            return True
        await asyncio.sleep(_POLL_INTERVAL_SEC)

    # Elevated to WARNING with last pane snippet — session won't start, user
    # sees generic error; this log is the only clue what CC was stuck on.
    last_lines = "\n".join(pane.splitlines()[-10:]) if pane else "<empty>"
    logger.warning(
        "TUI_IO: readiness timeout for session=%s after %.1fs, killing. Last pane lines:\n%s",
        session_name,
        timeout,
        last_lines,
    )
    await asyncio.to_thread(_kill_session, session_name)
    return False

#!/usr/bin/env python3
"""Minimal stand-in for the Antigravity CLI TUI, for tmux integration tests.

It reproduces only what the bot relies on: the trust dialog on first use, the
idle/busy footers, an input line between separators, ``--conversation`` resume,
and the append-only ``brain/<id>/.system_generated/logs/transcript_full.jsonl``.
State lives next to this script (``AGY_HOME``), so tests point the bot there.

Replies: a prompt containing ``IMAGE`` produces a generated-image step; any
other prompt is answered with ``echo: <prompt>``.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
import uuid
from pathlib import Path

HOME = Path(__file__).resolve().parent
SEP = "─" * 40


def transcript(cid: str) -> Path:
    path = HOME / "brain" / cid / ".system_generated" / "logs" / "transcript_full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append(cid: str, step: dict[str, object]) -> None:
    path = transcript(cid)
    index = sum(1 for _ in path.open()) if path.exists() else 0
    with path.open("a") as handle:
        handle.write(json.dumps({"step_index": index, "status": "DONE", **step}) + "\n")


def screen(footer: str, history: list[str]) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    for line in history[-20:]:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"{SEP}\n>\n{SEP}\n{footer}")
    # Park the cursor on the input line so typed characters echo there.
    sys.stdout.write("\x1b[2A\r\x1b[2C")
    sys.stdout.flush()


def read_message() -> str | None:
    """One submitted message: every line that arrives in one burst.

    A multi-line paste reaches a cooked-mode tty as several lines followed by
    the Enter that submits it; real agy treats that as one message.
    """
    data = b""
    while b"\n" not in data:
        chunk = os.read(0, 65536)
        if not chunk:
            return None
        data += chunk
    while select.select([0], [], [], 0.5)[0]:
        chunk = os.read(0, 65536)
        if not chunk:
            break
        data += chunk
    # Real agy consumes Escape (cancel) keystrokes; a cooked tty keeps them.
    return data.decode(errors="replace").replace("\x1b", "").strip()


def main() -> None:
    args = sys.argv[1:]
    cid = args[args.index("--conversation") + 1] if "--conversation" in args else None
    trusted = HOME / "trusted"
    if not trusted.exists():
        sys.stdout.write(
            "Do you trust the contents of this project?\n"
            "> Yes, I trust this folder\n  No, exit\n  ↑/↓ Navigate · enter Confirm"
        )
        sys.stdout.flush()
        read_message()
        trusted.touch()
    history: list[str] = ["Antigravity CLI (fake)"]
    screen("? for shortcuts", history)
    while (prompt := read_message()) is not None:
        if not prompt:
            screen("? for shortcuts", history)
            continue
        if cid is None:
            cid = str(uuid.uuid4())
        append(
            cid,
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": f"<USER_REQUEST>\n{prompt}\n</USER_REQUEST>",
            },
        )
        shown = prompt.splitlines()[-1]
        screen("esc to cancel", [*history, f"> {shown}"])
        time.sleep(0.3)
        if "IMAGE" in prompt:
            image = HOME / "brain" / cid / "picture_1.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            append(
                cid,
                {
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "content": "",
                    "tool_calls": [{"name": "generate_image", "args": {"ImageName": "picture"}}],
                },
            )
            append(
                cid,
                {
                    "source": "MODEL",
                    "type": "GENERIC",
                    "content": "Generated image is saved.",
                    "media": [{"mime_type": "image/png", "uri": f"file://{image}"}],
                },
            )
        reply = f"echo: {prompt.splitlines()[-1]}"
        append(cid, {"source": "MODEL", "type": "PLANNER_RESPONSE", "content": reply})
        history += [f"> {shown}", f"  {reply}"]
        screen("? for shortcuts", history)


if __name__ == "__main__":
    main()

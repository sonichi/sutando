#!/usr/bin/env python3
"""PreCompact + SessionStart(compact) hook: owner questions cross a compaction as rows, not prose.

A compaction summary carried the core's open owner decisions forward as text, and each turn
re-printed and grew the list. PreCompact stdout is appended to the summarizer's instructions,
so this tells it to leave owner questions out (the Pending questions database is their record)
and to keep any never-filed one only as a to-file item. After the compaction, SessionStart
(source "compact") adds the instruction to file those items through scripts/ask-owner.py,
which with --unless-open skips a question already open. Core sessions only; never blocks, never fails a hook.
"""
from __future__ import annotations

import json
import os
import select
import sys

TO_FILE = "Owner questions to file with ask-owner (not yet asked)"

SUMMARY_RULE = (
    "Owner questions are not carried in this summary: open owner questions are rows in the owner's "
    "Pending questions database, which is their record. Do not write a 'Decisions for you' list or "
    "any list of questions or decisions for the owner. Only an owner question from this conversation "
    "that was never filed with scripts/ask-owner.py may appear, once, under the heading "
    f"'{TO_FILE}', one line each.")

AFTER_COMPACT = (
    "Owner questions after this compaction: if the summary has the heading "
    f"'{TO_FILE}', file each item now with `python3 scripts/ask-owner.py \"<question>\" "
    "--unless-open [--context \"<why / options>\"]` (it files nothing already open), then drop the "
    "heading. Never list owner questions in terminal or summary text; file them. Open questions: "
    "`python3 src/pending_questions_reader.py summary`.")


def is_core_session(env=os.environ) -> bool:
    return env.get("SUTANDO_CORE_SESSION") == "1" and not env.get("SUTANDO_INSTANCE_ID")


def respond(payload: dict, env=os.environ) -> str:
    """What this hook prints for `payload`; "" for anything it does not own."""
    if not is_core_session(env):
        return ""
    event = payload.get("hook_event_name")
    if event == "PreCompact":
        return SUMMARY_RULE
    if event == "SessionStart" and payload.get("source") == "compact":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": AFTER_COMPACT}})
    return ""


def main() -> int:
    try:
        if not select.select([sys.stdin], [], [], 1.0)[0]:
            return 0
        payload = json.loads(sys.stdin.read() or "{}")
        out = respond(payload) if isinstance(payload, dict) else ""
        if out:
            sys.stdout.write(out)
    except Exception:  # noqa: BLE001 — a failing PreCompact hook can stall a compaction
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

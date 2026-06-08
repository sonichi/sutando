#!/usr/bin/env python3
"""Structural regression test for the thread-auto-seed gate fix (2026-06-06).

The discord-bridge auto-seeds an access.json entry for a Discord thread the
FIRST time the bot sees a message in that thread, inheriting allowFrom from
the parent channel. Before 2026-06-06 the gate was:

    if bot_mentioned and isinstance(message.channel, discord.Thread):
        ...auto-seed...

This left a gap: any thread's FIRST message that didn't @-mention the bot
was silently dropped (the thread never landed in access.json, so the next
load_channel_config saw `thread_id_str not in groups` and the bridge
suppressed it). Hit live 2026-05-25 on the ep013 thread when Chi's "start
from news candidate" message went unprocessed for ~2h.

The fix: remove `bot_mentioned and` from the gate. Cost is bounded — only
the FIRST message per thread incurs the read+write; subsequent messages hit
the `thread_id_str not in access_groups` early-out and proceed unchanged.

This test catches a regression that reintroduces the bot_mentioned gate.

Scope: STRUCTURAL — regex-matches src/discord-bridge.py. Does NOT import
the bridge (discord.py dep weight is huge). Mirrors the style of
`discord-bridge-allowlist.test.py`.

Guards:
  1. The auto-seed block exists (matched by the `thread_id_str not in
     access_groups` early-out string — a stable internal landmark).
  2. The auto-seed block is gated ONLY on `isinstance(message.channel,
     discord.Thread)` — NOT on `bot_mentioned and ...`. The regression case
     is a future refactor that re-adds the mention gate.

Run: python3 tests/discord-bridge-thread-auto-seed-ungate.test.py
Exit: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def _src() -> str:
    return BRIDGE.read_text()


def test_auto_seed_block_exists():
    """The auto-seed scaffolding must still exist."""
    src = _src()
    assert "thread_id_str not in access_groups" in src, \
        "auto-seed early-out marker missing — block may have been removed"


def test_gate_ungated_on_bot_mentioned():
    """The auto-seed gate must be `isinstance(..., discord.Thread)` only —
    NOT `bot_mentioned and isinstance(..., discord.Thread)`.
    """
    src = _src()
    # The exact regression we're guarding against:
    regression = re.compile(
        r"if\s+bot_mentioned\s+and\s+isinstance\(\s*message\.channel\s*,\s*discord\.Thread\s*\)"
    )
    assert not regression.search(src), \
        "REGRESSION: auto-seed gate re-introduced `bot_mentioned and` — " \
        "this re-opens the 2026-05-25 ep013-thread silent-drop class. " \
        "See pending-questions.md (2026-05-17 entry) for context."

    # The correct form must be present:
    correct = re.compile(
        r"if\s+isinstance\(\s*message\.channel\s*,\s*discord\.Thread\s*\)\s*:"
    )
    assert correct.search(src), \
        "auto-seed gate not found in expected form `if isinstance(message.channel, discord.Thread):`"


def main():
    tests = [test_auto_seed_block_exists, test_gate_ungated_on_bot_mentioned]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL: {t.__name__} — {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)

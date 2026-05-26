#!/usr/bin/env python3
"""Tests for skills/discord-voice/scripts/join_trigger.py — message_is_join_phrase.

Covers:
  - Core phrase-matching behavior (exact, case-insensitive, boundary, punctuation)
  - Mention-prefix stripping added in #1113 (bare @id, @! nick, @& role, multi-mention)
  - False-positive prevention with mention prefix (boundary check still applies)
  - Empty/whitespace-only inputs
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = REPO_ROOT / "skills" / "discord-voice" / "scripts" / "join_trigger.py"

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Load the module without side-effects
# ---------------------------------------------------------------------------
if not _SCRIPT.exists():
    fail("module exists", f"{_SCRIPT} not found")
    print("\n0/1 tests passed")
    sys.exit(1)

spec = importlib.util.spec_from_file_location("join_trigger", _SCRIPT)
mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(mod)  # type: ignore[union-attr]

message_is_join_phrase = mod.message_is_join_phrase
PHRASE = "za warudo"


# ---------------------------------------------------------------------------
# T1–T8 — core phrase-matching (pre-#1113 behavior preserved)
# ---------------------------------------------------------------------------
def check(label, text, expected, phrase=PHRASE):
    result = message_is_join_phrase(text, phrase)
    (ok if result == expected else fail)(label)


check("T1: exact match", "za warudo", True)
check("T2: case-insensitive", "ZA WARUDO", True)
check("T3: trailing punctuation !", "za warudo!", True)
check("T4: trailing comma+space", "za warudo, please", True)
check("T5: leading+trailing spaces", "  za warudo  ", True)
check("T6: false positive boundary (za warudonow)", "za warudonow", False)
check("T7: empty string", "", False)
check("T8: unrelated text", "hello world", False)

# ---------------------------------------------------------------------------
# T9–T16 — mention-prefix stripping (#1113)
# ---------------------------------------------------------------------------

BOT_ID   = "1494123456789012345"
ROLE_ID  = "1098765432109876543"
OTHER_ID = "9876543210123456789"


check("T9:  @bot_id prefix stripped", f"<@{BOT_ID}> za warudo", True)
check("T10: @!nick prefix stripped", f"<@!{BOT_ID}> za warudo", True)
check("T11: @&role prefix stripped", f"<@&{ROLE_ID}> za warudo", True)
check("T12: double-mention stripped", f"<@{BOT_ID}> <@{OTHER_ID}> za warudo", True)
check("T13: mention + trailing punct", f"<@{BOT_ID}> za warudo!", True)
check("T14: mention + trailing comma", f"<@{BOT_ID}> za warudo, now", True)
check("T15: only mention, no phrase", f"<@{BOT_ID}>", False)
check("T16: mention with wrong phrase", f"<@{BOT_ID}> hello world", False)

# ---------------------------------------------------------------------------
# T17–T19 — false-positive boundary with mention prefix
# ---------------------------------------------------------------------------
check("T17: mention + za warudonow (boundary)", f"<@{BOT_ID}> za warudonow", False)
check("T18: mention prefix only", f"<@{BOT_ID}> ", False)
check("T19: mention + phrase + underscore boundary", f"<@{BOT_ID}> za warudo_extra", False)

# ---------------------------------------------------------------------------
# T20 — whitespace-only after stripping
# ---------------------------------------------------------------------------
check("T20: whitespace-only text", "   ", False)

# ---------------------------------------------------------------------------
# T21–T22 — phrase variations
# ---------------------------------------------------------------------------
check("T21: single-word phrase exact", "go", True, "go")
check("T22: single-word phrase with mention", f"<@{BOT_ID}> go", True, "go")

# ---------------------------------------------------------------------------
# T23 — empty phrase falls back (no crash)
# ---------------------------------------------------------------------------
result = message_is_join_phrase("anything", "")
if result is False:
    ok("T23: empty phrase returns False (no crash)")
else:
    fail("T23: empty phrase should return False", f"got {result!r}")

# ---------------------------------------------------------------------------
# T24 — multiline message (newline after phrase)
# ---------------------------------------------------------------------------
check("T24: newline after phrase", f"za warudo\nsome extra text", True)
check("T25: mention + newline after phrase", f"<@{BOT_ID}> za warudo\nextra", True)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if FAIL:
    sys.exit(1)

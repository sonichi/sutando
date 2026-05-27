#!/usr/bin/env python3
"""Structural tests for the join-phrase magic-word fast-path in discord-bridge.py.

PR #1113 added an early exit before the requireMention guard so a bare
"za warudo" in a guild text channel (no @-mention) still fires the voice-spawn
for the owner. These tests verify the structural invariants that keep that
ordering correct — no import of discord.py needed.

Guards:
  1. _dv_message_is_join_phrase is imported or stubbed near the top of the file.
  2. The magic-word fast-path (join-trigger check) appears BEFORE the
     requireMention skip in _handle_discord_message.
  3. The fast-path is owner-gated via load_allowed().
  4. The fast-path calls _dv_handle_join_trigger (not a raw spawn).
  5. The fast-path ends with `return` so the requireMention gate is bypassed.
  6. The fast-path is wrapped in try/except (fail-open: bridge stays up on error).

Run: python3 tests/discord-bridge-join-phrase-fastpath.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"

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


if not BRIDGE.exists():
    fail("T0: discord-bridge.py exists")
    print("\n0/1 tests passed")
    sys.exit(1)

src = BRIDGE.read_text()
lines = src.splitlines()

ok("T0: discord-bridge.py exists")

# ---------------------------------------------------------------------------
# T1 — _dv_message_is_join_phrase is available (imported or stubbed)
# ---------------------------------------------------------------------------
has_import = "_dv_message_is_join_phrase" in src
if has_import:
    ok("T1: _dv_message_is_join_phrase is referenced in the bridge")
else:
    fail("T1: _dv_message_is_join_phrase not found in bridge source")

# ---------------------------------------------------------------------------
# T2 — fast-path exists: the join-phrase check is present before requireMention
# ---------------------------------------------------------------------------
# Find the line number of the join-trigger fast-path and the requireMention skip.
join_trigger_line = None
require_mention_skip_line = None

for i, line in enumerate(lines):
    if join_trigger_line is None and "_dv_message_is_join_phrase" in line and "load_allowed()" in line:
        join_trigger_line = i
    if require_mention_skip_line is None and "[skip] not mentioned" in line and "requireMention" in line:
        require_mention_skip_line = i

if join_trigger_line is not None and require_mention_skip_line is not None:
    if join_trigger_line < require_mention_skip_line:
        ok(f"T2: join-trigger fast-path (L{join_trigger_line+1}) precedes requireMention skip (L{require_mention_skip_line+1})")
    else:
        fail(f"T2: ordering wrong — join-trigger L{join_trigger_line+1} is AFTER requireMention skip L{require_mention_skip_line+1}")
elif join_trigger_line is None:
    fail("T2: join-trigger fast-path not found (missing load_allowed() + _dv_message_is_join_phrase on same line)")
else:
    fail("T2: requireMention skip line not found")

# ---------------------------------------------------------------------------
# T3 — fast-path is owner-gated via load_allowed()
# ---------------------------------------------------------------------------
# The fast-path condition must include load_allowed() so only the owner fires it.
if join_trigger_line is not None:
    fastpath_line = lines[join_trigger_line]
    if "load_allowed()" in fastpath_line:
        ok("T3: fast-path condition includes load_allowed() (owner-only gate)")
    else:
        fail("T3: fast-path condition missing load_allowed()", fastpath_line.strip())
else:
    fail("T3: fast-path not found, skipping owner-gate check")

# ---------------------------------------------------------------------------
# T4 — fast-path calls _dv_handle_join_trigger (not a raw subprocess spawn)
# ---------------------------------------------------------------------------
# Look in the ~20 lines following the fast-path condition.
if join_trigger_line is not None:
    block = "\n".join(lines[join_trigger_line : join_trigger_line + 25])
    if "_dv_handle_join_trigger" in block:
        ok("T4: fast-path calls _dv_handle_join_trigger (delegates to trigger handler)")
    else:
        fail("T4: _dv_handle_join_trigger not called in fast-path block", block[:200])
else:
    fail("T4: fast-path not found, skipping handler-call check")

# ---------------------------------------------------------------------------
# T5 — fast-path ends with `return` to bypass requireMention gate
# ---------------------------------------------------------------------------
if join_trigger_line is not None:
    block_lines = lines[join_trigger_line : join_trigger_line + 30]
    has_return = any(re.search(r'\breturn\b', l) for l in block_lines)
    if has_return:
        ok("T5: fast-path block contains `return` — requireMention gate is bypassed on match")
    else:
        fail("T5: no `return` found in fast-path block — requireMention gate NOT bypassed")
else:
    fail("T5: fast-path not found, skipping return check")

# ---------------------------------------------------------------------------
# T6 — fast-path is wrapped in try/except (fail-open)
# ---------------------------------------------------------------------------
if join_trigger_line is not None:
    # Look back a few lines for the `try:` that wraps the fast-path condition.
    context_start = max(0, join_trigger_line - 5)
    context = "\n".join(lines[context_start : join_trigger_line + 30])
    if "except" in context:
        ok("T6: fast-path is wrapped in try/except (bridge stays up on error)")
    else:
        fail("T6: fast-path has no try/except wrapper")
else:
    fail("T6: fast-path not found, skipping try/except check")

# ---------------------------------------------------------------------------
# T7 — _dv_handle_join_trigger is also defined/imported
# ---------------------------------------------------------------------------
if "_dv_handle_join_trigger" in src:
    ok("T7: _dv_handle_join_trigger is referenced in the bridge")
else:
    fail("T7: _dv_handle_join_trigger not found — fast-path calls an undefined function")

# ---------------------------------------------------------------------------
# T8 — load_allowed() function is defined in the bridge
# ---------------------------------------------------------------------------
if re.search(r'^def load_allowed\(\)', src, re.MULTILINE):
    ok("T8: load_allowed() function is defined in the bridge")
else:
    fail("T8: load_allowed() function definition not found")

# ---------------------------------------------------------------------------
# T9 — fast-path log line references requireMention so future readers can grep
# ---------------------------------------------------------------------------
if join_trigger_line is not None:
    block = "\n".join(lines[join_trigger_line : join_trigger_line + 15])
    if "requireMention" in block or "bypassing" in block:
        ok("T9: fast-path log line mentions requireMention/bypassing for grep-ability")
    else:
        fail("T9: fast-path log line doesn't mention requireMention or bypassing")
else:
    fail("T9: fast-path not found, skipping log-line check")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if FAIL:
    sys.exit(1)

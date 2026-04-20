#!/usr/bin/env python3
"""
Regression test for PR #481 follow-up: sibling bot posts to #bot2bot
should be classified as access_tier=team, not owner.

Before PR #481: bot-author messages were dropped at line 268 regardless
of access.json. After PR #481 + the access.json convergence: sibling-bot
bare posts in #bot2bot pass the bot-filter but could be classified as
access_tier=owner IF the sibling bot ID is in the GLOBAL `allowFrom`.

The chosen mitigation (option A from MacBook's 2026-04-20 proposal,
approved by Chi): drop sibling-bot IDs from the global allowFrom.
Channel-level allowFrom on #bot2bot still permits the bot; the tier
just downgrades from "owner" to "team".

This test guards the access_tier classification logic structurally.
It does NOT exercise the actual Discord flow — that would need a live
bridge + mocked discord.py objects.

Guards:
  1. access_tier starts as "other" (fail-closed default).
  2. The global `allowed` set maps to access_tier="owner".
  3. An else branch checks the union of channel-level allowFroms for
     access_tier="team".
  4. The classification comment references tier behavior so the intent
     isn't lost in a blind reformat.

Run: python3 tests/discord-bridge-access-tier.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def main() -> int:
    if not BRIDGE.exists():
        print(f"FAIL: {BRIDGE} not found", file=sys.stderr)
        return 1

    src = BRIDGE.read_text()

    # The access_tier determination block. Find it by the sentinel comment.
    match = re.search(
        r"# Determine access tier\s*\n([\s\S]{0,2000}?)(?=\n    # Dedup:|\n    # Deterministic tier|\n\ndef |\Z)",
        src,
    )
    if not match:
        print("FAIL: could not locate '# Determine access tier' block", file=sys.stderr)
        return 1
    block = match.group(1)

    # 1. access_tier starts as "other" (fail-closed).
    if not re.search(r'access_tier\s*=\s*[\'"]other[\'"]', block):
        print('FAIL: access_tier should default to "other" before the owner/team checks', file=sys.stderr)
        print("---block---", file=sys.stderr)
        print(block, file=sys.stderr)
        return 1

    # 2. if sender_id in allowed → owner
    if not re.search(r"if\s+sender_id\s+in\s+allowed\s*:\s*\n\s+access_tier\s*=\s*[\'\"]owner[\'\"]", block):
        print('FAIL: expected `if sender_id in allowed: access_tier = "owner"` pattern', file=sys.stderr)
        print("---block---", file=sys.stderr)
        print(block, file=sys.stderr)
        return 1

    # 3. else branch checks channel-level allowFroms → team
    if not re.search(r"team_ids\.update\s*\(\s*ch_cfg\.get\s*\(\s*[\'\"]allowFrom[\'\"]", block):
        print('FAIL: else branch should union channel allowFroms into team_ids', file=sys.stderr)
        print("---block---", file=sys.stderr)
        print(block, file=sys.stderr)
        return 1

    if not re.search(r"access_tier\s*=\s*[\'\"]team[\'\"]", block):
        print('FAIL: team tier assignment missing', file=sys.stderr)
        print("---block---", file=sys.stderr)
        print(block, file=sys.stderr)
        return 1

    # 4. Order of checks matters: global `allowed` FIRST, then team as fallback.
    owner_idx = block.find('access_tier = "owner"')
    team_idx = block.find('access_tier = "team"')
    if owner_idx < 0 or team_idx < 0 or owner_idx >= team_idx:
        print("FAIL: owner tier assignment must appear before team tier assignment in the block", file=sys.stderr)
        return 1

    print("PASS: discord-bridge.py access_tier classification looks correct.")
    print(f"  - defaults to 'other'")
    print(f"  - global allowFrom → owner")
    print(f"  - channel-level allowFrom union → team (fallback)")
    print(f"  - ordering enforced (owner check before team check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
  1. access_tier starts as "guest" (fail-closed default).
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
        r"# Determine access tier\s*\n([\s\S]{0,3500}?)(?=\n    # Dedup:|\n    # Deterministic tier|\n\ndef |\Z)",
        src,
    )
    if not match:
        print("FAIL: could not locate '# Determine access tier' block", file=sys.stderr)
        return 1
    block = match.group(1)

    # 1. access_tier starts as "guest" (fail-closed). "other" is the retired
    #    spelling — a revert to it must fail here, not just read oddly.
    if not re.search(r'access_tier\s*=\s*[\'"]guest[\'"]', block):
        print('FAIL: access_tier should default to "guest" before the owner/team checks', file=sys.stderr)
        print("---block---", file=sys.stderr)
        print(block, file=sys.stderr)
        return 1

    if re.search(r'access_tier\s*=\s*[\'"]other[\'"]', block):
        print('FAIL: the retired "other" spelling must not be assigned anywhere in the tier block', file=sys.stderr)
        return 1

    # 2. if sender_id in allowed → owner ONLY via tierMap (post-2026-07-17:
    #    allowlist membership no longer grants owner unconditionally; a seeded
    #    tierMap gates it, new additions default to team). Assert the gated form.
    if not re.search(r"if\s+sender_id\s+in\s+allowed\s*:", block):
        print('FAIL: expected `if sender_id in allowed:` guard', file=sys.stderr)
        print("---block---", file=sys.stderr); print(block, file=sys.stderr); return 1
    if "ensure_tier_map_seeded()" not in block or "load_tier_map()" not in block:
        print('FAIL: in-allowed branch must consult the seeded tierMap (default read-only)', file=sys.stderr)
        print("---block---", file=sys.stderr); print(block, file=sys.stderr); return 1

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

    # 4. Order of checks matters: the global `allowed` branch (which can yield
    #    owner via the seeded tierMap) is evaluated FIRST, then the channel
    #    team_ids fallback. Post-2026-07-17 owner is assigned via a .get()
    #    default rather than a literal, so anchor on the branch structure.
    allowed_idx = block.find('if sender_id in allowed:')
    team_idx = block.find('team_ids.update')
    if allowed_idx < 0 or team_idx < 0 or allowed_idx >= team_idx:
        print("FAIL: global-allowed branch must precede the channel team_ids fallback", file=sys.stderr)
        return 1

    print("PASS: discord-bridge.py access_tier classification looks correct.")
    print("  - defaults to 'guest'")
    print("  - global allowFrom → owner")
    print("  - channel-level allowFrom union → team (fallback)")
    print("  - ordering enforced (owner check before team check)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

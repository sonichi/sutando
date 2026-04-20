#!/usr/bin/env python3
"""
Validate access.json tier isolation for sibling bots.

Context: PR #481 made discord-bridge's bot-author filter channel-config-aware
so sibling bots can post in `#bot2bot` (role=bot2bot, requireMention=false).
The follow-up gap flagged by Mini in review: if a sibling-bot ID is ALSO in
the GLOBAL `allowFrom`, a bare #bot2bot post from that bot would be
classified as `access_tier=owner` instead of `access_tier=team` — elevating
sibling-bot messages to full owner capabilities instead of the sandboxed
team path.

This script asserts: each sibling-bot ID (passed via --bot-ids or
SUTANDO_SIBLING_BOT_IDS env var) MUST NOT appear in the global allowFrom.
The owner's user_id may legitimately appear in both global and channel
allowFrom; the invariant only applies to bot IDs you nominate.

Exit 0 on pass, 1 on violation. Prints the violating IDs + the fix.

Usage:
  python3 scripts/validate-access-tiers.py --bot-ids 1490412828065267872
  SUTANDO_SIBLING_BOT_IDS=1490412828065267872 python3 scripts/validate-access-tiers.py
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_PATH = Path.home() / ".claude" / "channels" / "discord" / "access.json"


def violations(data: dict, bot_ids: set[str]) -> list[str]:
    """Return [bot_id, ...] for bot_ids present in the global allowFrom."""
    global_allow = set(str(x) for x in data.get("allowFrom", []))
    return sorted(bot_ids & global_allow)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    ap.add_argument("--bot-ids", default="", help="comma-separated sibling-bot user_ids")
    args = ap.parse_args()

    raw = args.bot_ids or os.environ.get("SUTANDO_SIBLING_BOT_IDS", "")
    bot_ids = {s.strip() for s in raw.split(",") if s.strip()}
    if not bot_ids:
        print("no bot IDs provided — pass --bot-ids or set SUTANDO_SIBLING_BOT_IDS")
        return 0

    path = Path(args.path)
    if not path.exists():
        print(f"access.json not found at {path} — nothing to validate")
        return 0

    data = json.loads(path.read_text())
    bad = violations(data, bot_ids)
    if not bad:
        print(f"OK: {path} — no sibling-bot IDs in global allowFrom (checked {len(bot_ids)} id(s))")
        return 0

    print(f"FAIL: {path} — {len(bad)} sibling-bot ID(s) in global allowFrom:")
    for uid in bad:
        print(f"  - user_id={uid}")
    print()
    print("Fix: remove the listed user_id(s) from the top-level `allowFrom` in access.json.")
    print("Channel-level allowFrom on the bot2bot channel already permits them; the global")
    print("entry only serves to elevate bare posts from them to access_tier=owner, which")
    print("bypasses the sandboxed team-tier path intended for cross-bot coord.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

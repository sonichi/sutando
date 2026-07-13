#!/usr/bin/env python3
"""Read recent messages from a Discord channel via REST API.

Exits after printing — never starts a persistent bot connection.

Usage:
    python3 src/discord-read.py <channel_id> [--limit N] [--after MSG_ID]

Requires DISCORD_BOT_TOKEN in $CLAUDE_CONFIG_DIR/channels/discord/.env or env var.
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import claude_home_path  # noqa: E402

_env = claude_home_path("channels", "discord", ".env")
for line in (_env.read_text().splitlines() if _env.exists() else []):
    k, _, v = line.partition("=")
    if k.strip() == "DISCORD_BOT_TOKEN" and v.strip():
        os.environ.setdefault("DISCORD_BOT_TOKEN", v.strip())

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
if not TOKEN:
    print(f"Requires DISCORD_BOT_TOKEN in {_env}", file=sys.stderr)
    sys.exit(1)

import argparse
parser = argparse.ArgumentParser()
parser.add_argument("channel_id")
parser.add_argument("--limit", type=int, default=10, help="Per-call page size (Discord caps at 100). With --until this is the page size, not the total.")
parser.add_argument("--after", default=None, help="Snowflake ID — fetch messages after this ID (newer)")
parser.add_argument("--before", default=None, help="Snowflake ID — fetch messages before this ID (older), one page.")
parser.add_argument("--until", default=None, help="Snowflake ID or ISO date/time (e.g. 2026-06-24T23:25) — page BACKWARD until reaching this boundary, then stop. Condition-based depth, NOT a message count: use to reconstruct context however far back the referent / conversational boundary is.")
args = parser.parse_args()

HEADERS = {"Authorization": f"Bot {TOKEN}", "User-Agent": "Sutando-reader/1.0"}
PAGE = min(max(args.limit, 1), 100)
# Runaway backstop only (not a depth target — depth is governed by --until):
# 200 pages * 100 = 20k messages before we refuse to loop forever.
MAX_PAGES = 200


def _fetch(extra):
    p = {"limit": str(PAGE)}
    p.update({k: v for k, v in extra.items() if v})
    url = f"https://discord.com/api/v10/channels/{args.channel_id}/messages?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _at_or_before_boundary(msg):
    """True once a message is at/older-than --until (id or ISO prefix)."""
    u = args.until
    if u.isdigit():
        try:
            return int(msg["id"]) <= int(u)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(u)] <= u


def _strictly_older_than_boundary(msg):
    u = args.until
    if u.isdigit():
        try:
            return int(msg["id"]) < int(u)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(u)] < u


try:
    if args.until:
        collected = []
        cursor = args.before  # None => start from latest
        for _ in range(MAX_PAGES):
            batch = _fetch({"before": cursor} if cursor else {})
            if not batch:
                break
            collected.extend(batch)
            cursor = batch[-1]["id"]
            if any(_at_or_before_boundary(m) for m in batch):
                break
        messages = collected
    else:
        messages = _fetch({"after": args.after, "before": args.before})
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

# Oldest first (snowflake id is time-ordered). Trim anything strictly older
# than the --until boundary so the output stops exactly where requested.
for msg in sorted(messages, key=lambda m: int(m["id"])):
    if args.until and _strictly_older_than_boundary(msg):
        continue
    author = msg.get("author", {}).get("username", "?")
    content = msg.get("content", "")[:200]
    ts = msg.get("timestamp", "")[:19]
    print(f"[{ts}] {author}: {content}")

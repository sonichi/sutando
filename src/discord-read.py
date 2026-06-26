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
parser.add_argument("--limit", type=int, default=10)
parser.add_argument("--after", default=None, help="Snowflake ID — fetch messages after this ID")
args = parser.parse_args()

params = {"limit": str(args.limit)}
if args.after:
    params["after"] = args.after
url = f"https://discord.com/api/v10/channels/{args.channel_id}/messages?" + urllib.parse.urlencode(params)
req = urllib.request.Request(url, headers={
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "Sutando-reader/1.0",
})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        messages = json.loads(r.read())
except Exception as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

for msg in reversed(messages):  # oldest first
    author = msg.get("author", {}).get("username", "?")
    content = msg.get("content", "")[:200]
    ts = msg.get("timestamp", "")[:19]
    print(f"[{ts}] {author}: {content}")

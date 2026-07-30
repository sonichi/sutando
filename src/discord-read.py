#!/usr/bin/env python3
"""Read recent messages from a Discord channel via REST API.

Exits after printing — never starts a persistent bot connection.

Usage:
    python3 src/discord-read.py <channel_id> [--limit N] [--after MSG_ID]

Requires DISCORD_BOT_TOKEN in $CLAUDE_CONFIG_DIR/channels/discord/.env or env var.
"""
import argparse
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import claude_home_path  # noqa: E402
from discord_http import request_json  # noqa: E402

# Runaway backstop only (not a depth target — depth is governed by --until):
# 200 pages * 100 = 20k messages before we refuse to loop forever.
MAX_PAGES = 200


def _load_token(env):
    """Populate DISCORD_BOT_TOKEN from the channel .env (if present) and return it."""
    for line in (env.read_text().splitlines() if env.exists() else []):
        k, _, v = line.partition("=")
        if k.strip() == "DISCORD_BOT_TOKEN" and v.strip():
            os.environ.setdefault("DISCORD_BOT_TOKEN", v.strip())
    return os.environ.get("DISCORD_BOT_TOKEN", "")


def _fetch(extra, channel_id, page, headers):
    p = {"limit": str(page)}
    p.update({k: v for k, v in extra.items() if v})
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url, headers=headers)
    # request_json honors 429 Retry-After + retries transient 5xx, so a rate
    # limit mid-pagination no longer aborts the read (2026-07-24 truncation fix).
    return request_json(req, timeout=10)


def _at_or_before_boundary(msg, until):
    """True once a message is at/older-than --until (id or ISO prefix)."""
    if until.isdigit():
        try:
            return int(msg["id"]) <= int(until)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(until)] <= until


def _strictly_older_than_boundary(msg, until):
    if until.isdigit():
        try:
            return int(msg["id"]) < int(until)
        except (KeyError, ValueError):
            return False
    return (msg.get("timestamp", "") or "")[:len(until)] < until


def _parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("channel_id")
    parser.add_argument("--limit", type=int, default=10, help="Per-call page size (Discord caps at 100). With --until this is the page size, not the total.")
    parser.add_argument("--after", default=None, help="Snowflake ID — fetch messages after this ID (newer)")
    parser.add_argument("--before", default=None, help="Snowflake ID — fetch messages before this ID (older), one page.")
    parser.add_argument("--until", default=None, help="Snowflake ID or ISO date/time (e.g. 2026-06-24T23:25) — page BACKWARD until reaching this boundary, then stop. Condition-based depth, NOT a message count: use to reconstruct context however far back the referent / conversational boundary is.")
    return parser.parse_args(argv)


def main(argv=None):
    env = claude_home_path("channels", "discord", ".env")
    token = _load_token(env)
    if not token:
        print(f"Requires DISCORD_BOT_TOKEN in {env}", file=sys.stderr)
        return 1

    args = _parse_args(argv)
    headers = {"Authorization": f"Bot {token}", "User-Agent": "Sutando-reader/1.0"}
    page = min(max(args.limit, 1), 100)

    try:
        if args.until:
            collected = []
            cursor = args.before  # None => start from latest
            for _ in range(MAX_PAGES):
                batch = _fetch({"before": cursor} if cursor else {}, args.channel_id, page, headers)
                if not batch:
                    break
                collected.extend(batch)
                cursor = batch[-1]["id"]
                if any(_at_or_before_boundary(m, args.until) for m in batch):
                    break
            messages = collected
        else:
            messages = _fetch({"after": args.after, "before": args.before}, args.channel_id, page, headers)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Oldest first (snowflake id is time-ordered). Trim anything strictly older
    # than the --until boundary so the output stops exactly where requested.
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        if args.until and _strictly_older_than_boundary(msg, args.until):
            continue
        author = msg.get("author", {}).get("username", "?")
        content = msg.get("content", "")[:200]
        ts = msg.get("timestamp", "")[:19]
        print(f"[{ts}] {author}: {content}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

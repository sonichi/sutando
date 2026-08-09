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

# Ordinary messages are clipped so a long scroll stays scannable; FORWARDS are
# exempt (see _render) because a forward is usually the substance, not chatter.
CLIP = 200
# Reply targets are clipped harder than bodies: the point is to identify WHICH
# message is being answered, not to re-read it.
REPLY_CLIP = 110


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


def _reply_context(msg):
    """The message this one is REPLYING to, or None.

    A terse reply is uninterpretable without its target. Measured 2026-08-04 in
    the owner channel: `2 merge` and `2y\n3 I didn't delete it.` are both
    replies, and both rendered here as bare text with nothing indicating what
    they answered. Read from the channel alone they are unreadable; they only
    parsed because the task file happened to carry a `[Replying to ...]` block,
    which exists only when a message becomes a task.

    That matters because this is the reader `context-reconstruct` runs on every
    pass — the same reason the forward bug above was worth fixing. A peer hit
    the consequence directly: it re-asked an owner the same question twice after
    missing that a bare `2` was the answer to an enumerated question.

    LABELLED, never inlined — for the same reason forwards are labelled:
    attributing a quoted message to the replier is its own misreading.
    """
    ref = msg.get("referenced_message")
    if not isinstance(ref, dict):
        return None
    author = (ref.get("author") or {}).get("username", "?")
    body = " ".join((_render(ref) or "").split())
    return f"\u21b3 replying to {author}: {body[:REPLY_CLIP]}" if body else \
           f"\u21b3 replying to {author}: (no readable body)"


def _render(msg):
    """One message's readable body, INCLUDING forwarded content.

    A Discord *forward* carries empty top-level `content` and puts the real
    payload in `message_snapshots[0].message` (the bridge already knows this —
    `discord-bridge.py` reads it, `discord_addressee.py` documents it). This
    reader did not, so every forwarded message rendered as a BLANK LINE — and
    this is the reader the context-reconstruct step runs on every pass.

    Measured 2026-07-31: two forwards in #research-eval printed as empty while
    holding the only record of a live AMA failure (a 1,602-char transcript plus
    the reporter's own words). They were nearly deleted on the strength of that
    blank output — see feedback_forwarded_discord_msgs_hide_content_in_message_snapshots.

    The forward is LABELLED rather than silently inlined: attributing a quoted
    message to the forwarder is its own misreading. Forwards are also exempt
    from the 200-char clip — the clip exists to keep ordinary chatter scannable,
    and a forward is usually carrying the substance someone moved deliberately.
    """
    body = (msg.get("content") or "").strip()
    snaps = msg.get("message_snapshots") or []
    if not snaps:
        return body[:CLIP]
    fwd = (snaps[0].get("message") or {})
    fwd_body = (fwd.get("content") or "").strip()
    extra = []
    for a in fwd.get("attachments") or []:
        extra.append(f"<attachment: {a.get('filename', '?')}>")
    for e in fwd.get("embeds") or []:
        extra.append(f"<embed: {e.get('title') or e.get('type') or '?'}>")
    inner = " ".join(x for x in (fwd_body, *extra) if x) or "(forward with no readable body)"
    prefix = f"{body} " if body else ""
    return f"{prefix}[forwarded] {inner}"


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
        ts = msg.get("timestamp", "")[:19]
        print(f"[{ts}] {author}: {_render(msg)}")
        ctx = _reply_context(msg)
        if ctx:
            print(f"    {ctx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

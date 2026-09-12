#!/usr/bin/env python3
"""Read recent messages from a Discord channel via REST API.

Exits after printing — never starts a persistent bot connection.

Usage:
    python3 src/discord-read.py <channel_id> [--limit N] [--after MSG_ID] [--serving CH]

One of --serving or --operator is REQUIRED (exit 3 otherwise, nothing
fetched). --serving <origin_channel_id> runs the contextNotFrom gate BEFORE any
fetch (exit 2 on block) — same contract as read_discord_channel.py; pass the
task's origin channel when serving a task. --operator is the explicit
privileged mode for the core's own monitoring (no serving context exists);
choosing it is visible in the invocation, not a silent default.

Requires DISCORD_BOT_TOKEN in $CLAUDE_CONFIG_DIR/channels/discord/.env or env var.

--jsonl prints one JSON object per message (id, ts, author, text, reply, reply_to_id, url) instead of the
text lines, for a consumer that needs to link back to the message (the owner's triage card).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util_paths import claude_home_path  # noqa: E402
import policy.context.discord as discord_context_policy  # noqa: E402
from channels.discord.http import request_json  # noqa: E402

# Shared implementations, bound as module globals so tests patch them per-CLI.
import channels.discord.reader as _reader  # noqa: E402
from channels.discord.reader import (  # noqa: E402,F401
    CLIP, MAX_PAGES, REPLY_CLIP,
    _at_or_before_boundary, _redact, _render, _reply_context,
    _strictly_older_than_boundary,
)


def _fetch(extra, channel_id, page, headers):
    # rj resolves through THIS module's global at call time (patchable per-CLI).
    return _reader._fetch(extra, channel_id, page, headers, rj=request_json)


def _load_token(env):
    """Resolve DISCORD_BOT_TOKEN via the shared policy: env -> `env` file -> vault."""
    from channel_token import resolve_channel_token
    return resolve_channel_token("DISCORD_BOT_TOKEN", env_file=env)


def _parse_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("channel_id")
    parser.add_argument("--limit", type=int, default=10, help="Per-call page size (Discord caps at 100). With --until this is the page size, not the total.")
    parser.add_argument("--after", default=None, help="Snowflake ID — fetch messages after this ID (newer)")
    parser.add_argument("--before", default=None, help="Snowflake ID — fetch messages before this ID (older), one page.")
    parser.add_argument("--full", action="store_true",
                        help="Do not clip bodies. Use when the read is a VERIFICATION instrument ('did my message land?') rather than a scan: a grep past the 200-char clip returns 0 for text that WAS delivered, and a false negative there causes a duplicate send.")
    parser.add_argument("--until", default=None, help="Snowflake ID or ISO date/time (e.g. 2026-06-24T23:25) — page BACKWARD until reaching this boundary, then stop. Condition-based depth, NOT a message count: use to reconstruct context however far back the referent / conversational boundary is.")
    parser.add_argument("--jsonl", action="store_true",
                        help="One JSON object per message (id, ts, author, text, reply, reply_to_id, url) instead of the text lines. url is the message's jump link; it costs one channel lookup for the guild id.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serving", default=None,
                      help="Origin channel_id of the task being served. Runs the contextNotFrom gate BEFORE any fetch; exit 2 on block.")
    mode.add_argument("--operator", action="store_true",
                      help="Explicit operator mode: no serving context (core monitoring). Mutually exclusive with --serving.")
    return parser.parse_args(argv)


REPLY_MESSAGE_TYPE = 19   # Discord message type: an inline reply


def _reply_to_id(msg):
    """The parent's id, for a REPLY only.

    Two independent facts are needed and neither alone is enough. The enclosing
    message TYPE says this is a reply (19); `message_reference` says what it
    points at. `reference.type` 0 is DEFAULT, which Discord also uses for
    crossposts and pins -- both carry `{type: 0, message_id: ...}` with no
    embedded parent, so keying on it invents a reply edge for a syndicated post
    or a pin notification. And `referenced_message` is optional: omitted when
    unfetched, null when the parent was deleted, which is when a key matters.

    Deliberately excluded, each referencing a message it is not replying to:
    THREAD_STARTER_MESSAGE (21) points at the message a thread grew from, and
    CONTEXT_MENU_COMMAND (23) at the command's target.
    """
    if msg.get("type") != REPLY_MESSAGE_TYPE:
        return ""
    ref = msg.get("message_reference") or {}
    if ref.get("type", 0) != 0:          # 1 is FORWARD
        return ""
    mid = ref.get("message_id") or (msg.get("referenced_message") or {}).get("id") or ""
    return str(mid)


def main(argv=None):
    env = claude_home_path("channels", "discord", ".env")
    token = _load_token(env)
    if not token:
        print(f"Requires DISCORD_BOT_TOKEN in {env}", file=sys.stderr)
        return 1

    args = _parse_args(argv)

    if not args.serving and not args.operator:
        print("Refusing: pass --serving <origin_channel_id> (task-serving read, "
              "gated) or --operator (explicit privileged read). A bare read was "
              "the contextNotFrom bypass. Nothing was fetched.", file=sys.stderr)
        return 3

    if args.serving:
        reason = discord_context_policy.gate(args.serving, args.channel_id, token)
        if reason is not None:
            print(f"BLOCKED: {reason}. Refusing to add its content to context "
                  f"(contextNotFrom rule). Nothing was fetched.")
            return 2

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

    # A jump link needs the guild; a DM channel has none and Discord spells that "@me".
    guild = discord_context_policy.resolve_guild(args.channel_id, token) if args.jsonl else None
    # Oldest first (snowflake id is time-ordered). Trim anything strictly older
    # than the --until boundary so the output stops exactly where requested.
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        if args.until and _strictly_older_than_boundary(msg, args.until):
            continue
        author = msg.get("author", {}).get("username", "?")
        ts = msg.get("timestamp", "")[:19]
        clip = None if args.full else CLIP
        ctx = _reply_context(msg, None if args.full else REPLY_CLIP)
        if args.jsonl:
            print(json.dumps({
                "id": str(msg.get("id", "")), "ts": ts, "author": author,
                "text": _render(msg, clip), "reply": ctx or "",
                # `reply` is clipped at REPLY_CLIP, so matching its text picks
                # the wrong parent silently once a parent is longer. A key cannot.
                "reply_to_id": _reply_to_id(msg),
                "url": f"https://discord.com/channels/{guild or '@me'}/{args.channel_id}/{msg.get('id', '')}",
            }, ensure_ascii=False))
            continue
        print(f"[{ts}] {author}: {_render(msg, clip)}")
        if ctx:
            print(f"    {ctx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

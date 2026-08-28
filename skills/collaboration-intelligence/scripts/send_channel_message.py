#!/usr/bin/env python3
"""Post one message to a named Discord channel and prove it carried a mention.

Separate from bot2bot-post because that tool always resolves the bot2bot
channel: routing a reviewer notification through it validated one channel and
delivered to another.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

API = "https://discord.com/api/v10"
UA = "Sutando (https://github.com/sonichi/sutando, 1.0)"


def token() -> str:
    base = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or (pathlib.Path.home() / ".claude"))
    for line in (base / "channels" / "discord" / ".env").read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("send_channel_message: DISCORD_BOT_TOKEN not found")


def post(channel: str, body: str) -> dict:
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages", method="POST",
        data=json.dumps({"content": body}).encode(),
        headers={"Authorization": f"Bot {token()}", "Content-Type": "application/json",
                 "User-Agent": UA})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: send_channel_message.py <channel-id> <body>", file=sys.stderr)
        return 2
    channel, body = argv
    posted = post(channel, body)
    # A mention is the delivery mechanism, and the API reports 200 either way.
    # Read it back from the POSTED body, never from the string we sent.
    if not (posted.get("mentions") or []):
        print(f"send_channel_message: posted {posted.get('id')} to {channel} but its "
              "mentions array is EMPTY — it notified nobody", file=sys.stderr)
        return 3
    print(posted["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Send a task result to Discord DM if voice client is disconnected.

Usage:
    python3 src/dm-result.py "Result text here"
    python3 src/dm-result.py --file results/task-123.txt

Checks http://localhost:8080/sse-status for voiceConnected.
If voice is connected, does nothing (voice agent will speak the result).
If voice is disconnected, sends the result to the owner's Discord DM.

Requires DISCORD_TOKEN in .env and the Discord bridge running.
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DM_CHANNEL = "1485370959870431433"  # Owner's Discord DM channel
SSE_STATUS_URL = "http://localhost:8080/sse-status"


def voice_connected() -> bool:
    """Check if a voice client is currently connected."""
    try:
        with urllib.request.urlopen(SSE_STATUS_URL, timeout=2) as resp:
            data = json.loads(resp.read())
            return data.get("voiceConnected", False)
    except Exception:
        return False


def send_dm(text: str) -> bool:
    """Send text to owner's Discord DM via the MCP Discord bridge."""
    # Use the discord bridge's reply tool via a task file approach,
    # or call the Discord API directly if bot token is available.
    # Check both locations for the Discord bot token
    token = ""
    for env_path in [
        Path.home() / ".claude" / "channels" / "discord" / ".env",
        REPO / ".env",
    ]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DISCORD_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if token:
            break

    if not token:
        print("dm-result: DISCORD_TOKEN not found in .env", file=sys.stderr)
        return False

    url = f"https://discord.com/api/v10/channels/{DM_CHANNEL}/messages"
    # Truncate if too long for Discord (2000 char limit)
    if len(text) > 1900:
        text = text[:1900] + "\n... (truncated)"

    payload = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Sutando/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                print(f"dm-result: sent to DM ({len(text)} chars)")
                return True
            else:
                print(f"dm-result: Discord returned {resp.status}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"dm-result: failed to send DM: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 src/dm-result.py 'text' | --file path", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--file":
        if len(sys.argv) < 3:
            print("Usage: python3 src/dm-result.py --file path", file=sys.stderr)
            sys.exit(1)
        text = Path(sys.argv[2]).read_text().strip()
    else:
        text = " ".join(sys.argv[1:])

    if voice_connected():
        print("dm-result: voice client connected, skipping DM (voice will deliver)")
        return

    print("dm-result: voice client disconnected, sending to Discord DM")
    if send_dm(text):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

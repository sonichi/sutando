#!/usr/bin/env python3
"""Send a message to a Discord channel or DM via gateway.

Usage:
  python3 src/discord-send.py <channel_id> "message"
  python3 src/discord-send.py <channel_id> "message" --reply-to <message_id>
  python3 src/discord-send.py --dm <user_id> "message"
"""
import asyncio
import os
import sys
from pathlib import Path

import discord

# Load token
env_file = Path.home() / ".claude" / "channels" / "discord" / ".env"
TOKEN = ""
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()

if not TOKEN:
    print("DISCORD_BOT_TOKEN not set")
    sys.exit(1)


async def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    dm_mode = args[0] == "--dm"
    if dm_mode:
        target_id = int(args[1])
        message = args[2]
        reply_to = None
    else:
        target_id = int(args[0])
        message = args[1]
        reply_to = int(args[args.index("--reply-to") + 1]) if "--reply-to" in args else None

    client = discord.Client(intents=discord.Intents.default())

    @client.event
    async def on_ready():
        try:
            if dm_mode:
                user = await client.fetch_user(target_id)
                target = await user.create_dm()
            else:
                target = client.get_channel(target_id)

            if not target:
                print(f"Target {target_id} not found")
                await client.close()
                return

            ref = None
            if reply_to:
                ref = await target.fetch_message(reply_to)

            for i in range(0, len(message), 1900):
                await target.send(message[i:i+1900], reference=ref)
                ref = None  # only reply on first chunk

            print("sent")
        except Exception as e:
            print(f"error: {e}")
        await client.close()

    await client.start(TOKEN)


asyncio.run(main())

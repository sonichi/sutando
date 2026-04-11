#!/bin/bash
# Sutando: notify the user across available channels
# Usage: bash src/notify.sh "message"
#
# Channels (in order):
# 1. Voice (results/proactive-*.txt) — if voice client is connected
# 2. Discord DM — always
# 3. macOS notification — always (local only)

MSG="$1"
if [ -z "$MSG" ]; then echo "Usage: bash src/notify.sh 'message'"; exit 1; fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TS=$(date +%s%3N)

# Load tokens from channel configs
DISCORD_TOKEN=$(grep DISCORD_BOT_TOKEN ~/.claude/channels/discord/.env 2>/dev/null | cut -d= -f2-)
DISCORD_USER_ID=$(python3 -c "import json; print(json.load(open('$HOME/.claude/channels/discord/access.json')).get('allowFrom',[''])[0])" 2>/dev/null)

# 1. Voice — write proactive message if voice agent is up
if curl -s -o /dev/null -w "%{http_code}" http://localhost:9900 2>/dev/null | grep -q "426"; then
  echo "$MSG" > "$REPO_DIR/results/proactive-$TS.txt"
fi

# 2. Discord DM
if [ -n "$DISCORD_TOKEN" ]; then
  DM_CHANNEL=$(curl -s -X POST "https://discord.com/api/v10/users/@me/channels" \
    -H "Authorization: Bot $DISCORD_TOKEN" \
    -H "Content-Type: application/json" \
    -H "User-Agent: DiscordBot (https://github.com/sonichi/sutando, 1.0)" \
    -d "{\"recipient_id\":\"$DISCORD_USER_ID\"}" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)
  if [ -n "$DM_CHANNEL" ]; then
    curl -s -X POST "https://discord.com/api/v10/channels/$DM_CHANNEL/messages" \
      -H "Authorization: Bot $DISCORD_TOKEN" \
      -H "Content-Type: application/json" \
      -H "User-Agent: DiscordBot (https://github.com/sonichi/sutando, 1.0)" \
      -d "{\"content\":\"$MSG\"}" >/dev/null 2>&1
  fi
fi

# 3. macOS notification
osascript -e "display notification \"$MSG\" with title \"Sutando\"" 2>/dev/null

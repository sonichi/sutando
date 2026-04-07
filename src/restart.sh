#!/bin/bash
# Sutando restart — stops all background services, then restarts via startup.sh.
# Does NOT touch the Claude Code CLI (core agent) — that's managed separately.
# Usage: bash src/restart.sh
#   --stop-only    Stop without restarting

REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "Stopping Sutando services..."
pkill -f "voice-agent" 2>/dev/null
pkill -f "web-client.ts" 2>/dev/null
pkill -f "dashboard.py" 2>/dev/null
pkill -f "agent-api.py" 2>/dev/null
pkill -f "screen-capture-server" 2>/dev/null
pkill -f "telegram-bridge" 2>/dev/null
pkill -f "discord-bridge" 2>/dev/null
pkill -f "watch-tasks" 2>/dev/null
pkill -f "conversation-server" 2>/dev/null
pkill -f "ngrok" 2>/dev/null
pkill -f "credential-proxy" 2>/dev/null
pkill -f "src/Sutando/Sutando" 2>/dev/null
echo "  All services stopped"

if [ "$1" = "--stop-only" ]; then
    echo "Done. Run 'bash src/startup.sh' to start again."
    exit 0
fi

sleep 1

echo "Starting..."
exec bash "$REPO/src/startup.sh"

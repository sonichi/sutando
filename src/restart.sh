#!/bin/bash
# Sutando restart — kills all services and restarts them.
# Usage: bash src/restart.sh

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
pkill -f "src/Sutando/Sutando" 2>/dev/null

# Clear stale results to prevent flood on restart
rm -f "$REPO"/results/*.txt 2>/dev/null
echo "  Cleared stale results"

sleep 1

echo "Starting..."
exec bash "$REPO/src/startup.sh"

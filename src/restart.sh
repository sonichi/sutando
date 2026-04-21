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

# Wait for shutdown to drain before exec-ing startup.sh. Fixed `sleep 1`
# raced the pkill'd processes: if Sutando.app (or any SIGTERM-respecting
# service) took >1s to exit cleanly, startup.sh's `if ! pgrep ...` guard
# skipped the relaunch and the user saw "restart did nothing."
# See feedback_pkill_then_open_race.md and PR #499 for the same class on
# startup.sh's recompile-replace path.
STOP_PATTERNS=(
    "voice-agent" "web-client.ts" "dashboard.py" "agent-api.py"
    "screen-capture-server" "telegram-bridge" "discord-bridge" "watch-tasks"
    "conversation-server" "ngrok" "credential-proxy" "src/Sutando/Sutando"
)
for _ in $(seq 1 30); do
    still=0
    for pat in "${STOP_PATTERNS[@]}"; do
        if pgrep -f "$pat" >/dev/null 2>&1; then still=1; break; fi
    done
    [ $still -eq 0 ] && break
    sleep 0.1
done

echo "Starting..."
exec bash "$REPO/src/startup.sh"

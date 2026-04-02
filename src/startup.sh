#!/bin/bash
# Sutando startup — starts all services + Claude Code.
# Usage: bash src/startup.sh

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "Sutando startup..."
echo ""

# Install dependencies if needed
[ -d node_modules ] || npm install

# Check prerequisites
missing=0
if ! command -v node > /dev/null 2>&1; then echo "  ✗ node not found — brew install node"; missing=1; fi
if ! command -v npx > /dev/null 2>&1; then echo "  ✗ npx not found — comes with node"; missing=1; fi
if ! command -v python3 > /dev/null 2>&1; then echo "  ✗ python3 not found"; missing=1; fi
if ! command -v claude > /dev/null 2>&1; then echo "  ✗ claude not found — see https://docs.anthropic.com/en/docs/claude-code/getting-started"; missing=1; fi
if ! command -v fswatch > /dev/null 2>&1; then echo "  ✗ fswatch not found — brew install fswatch"; missing=1; fi
if [ ! -f .env ]; then echo "  ✗ .env not found — cp .env.example .env and add your keys"; missing=1; fi
# Load .env and check required keys
if [ -f .env ]; then
  set -a; source .env; set +a
  if [ -z "$GEMINI_API_KEY" ]; then echo "  ✗ GEMINI_API_KEY not set in .env — get one at https://ai.google.dev"; missing=1; fi
fi
if [ $missing -eq 1 ]; then echo ""; echo "Fix the above and try again."; exit 1; fi

# Check macOS permissions (can't grant programmatically, just warn)
echo "Checking permissions..."
if ! screencapture -x /tmp/sutando-permcheck.png 2>/dev/null; then
  echo "  ⚠ Screen Recording not granted"
  echo "    → System Settings → Privacy & Security → Screen & System Audio Recording"
  echo "    → Add 'claude' and 'node'"
else
  rm -f /tmp/sutando-permcheck.png
  echo "  ✓ Screen Recording"
fi

# Check Accessibility (needed for context drop shortcut)
if ! osascript -e 'tell application "System Events" to get name of first process whose frontmost is true' > /dev/null 2>&1; then
  echo "  ⚠ Accessibility not granted"
  echo "    → System Settings → Privacy & Security → Accessibility"
  echo "    → Add Terminal.app or Shortcuts.app"
else
  echo "  ✓ Accessibility"
fi
echo ""

# Install Claude Code skills (runs every startup, idempotent)
bash "$REPO/skills/install.sh" 2>/dev/null || true

# Create tasks/ and results/ directories
mkdir -p tasks results

# 0. Credential proxy for quota tracking (port 7846)
if ! lsof -i :7846 > /dev/null 2>&1; then
  echo "  Starting credential proxy (port 7846)..."
  npx tsx ~/.claude/skills/quota-tracker/scripts/credential-proxy.ts > /tmp/credential-proxy.log 2>&1 &
  sleep 1
  echo "  ✓ credential proxy"
else
  echo "  ✓ credential proxy (already running)"
fi
export ANTHROPIC_BASE_URL=http://localhost:7846

# 1. Voice agent (Gemini Live on port 9900)
if ! lsof -i :9900 > /dev/null 2>&1; then
  echo "  Starting voice agent (port 9900)..."
  npx tsx src/voice-agent.ts > src/voice-agent.log 2>&1 &
  echo "  ✓ voice agent"
else
  echo "  ✓ voice agent (already running)"
fi

# 2. Web client (port 8080)
if ! lsof -i :8080 > /dev/null 2>&1; then
  echo "  Starting web client (port 8080)..."
  npx tsx src/web-client.ts > src/web-client.log 2>&1 &
  echo "  ✓ web client"
else
  echo "  ✓ web client (already running)"
fi

# 3. Dashboard (port 7844)
if ! lsof -i :7844 > /dev/null 2>&1; then
  echo "  Starting dashboard (port 7844)..."
  python3 src/dashboard.py > src/dashboard.log 2>&1 &
  echo "  ✓ dashboard"
else
  echo "  ✓ dashboard (already running)"
fi

# 4. Agent API (port 7843)
if ! lsof -i :7843 > /dev/null 2>&1; then
  echo "  Starting agent API (port 7843)..."
  python3 src/agent-api.py > src/agent-api.log 2>&1 &
  echo "  ✓ agent API"
else
  echo "  ✓ agent API (already running)"
fi

# 5. Screen capture server (port 7845)
if ! lsof -i :7845 > /dev/null 2>&1; then
  echo "  Starting screen capture (port 7845)..."
  python3 src/screen-capture-server.py > src/screen-capture.log 2>&1 &
  echo "  ✓ screen capture"
else
  echo "  ✓ screen capture (already running)"
fi

# 5b. Sutando context drop app (global hotkey ⌃C)
if ! pgrep -f "Sutando" > /dev/null 2>&1; then
  if [ -f "$REPO/src/Sutando/Sutando" ]; then
    echo "  Starting Sutando..."
    "$REPO/src/Sutando/Sutando" > /dev/null 2>&1 &
    echo "  ✓ Sutando (⌃C to drop context)"
  else
    echo "  ⚠ Sutando not compiled — run: cd src/Sutando && swiftc -o Sutando main.swift -framework Cocoa -framework Carbon -framework ApplicationServices"
  fi
else
  echo "  ✓ Sutando (already running)"
fi

echo ""

# 6. Telegram bridge (optional — needs TELEGRAM_BOT_TOKEN)
if [ -f "$HOME/.claude/channels/telegram/.env" ] && grep -q "TELEGRAM_BOT_TOKEN=" "$HOME/.claude/channels/telegram/.env" 2>/dev/null; then
  if ! pgrep -f "telegram-bridge" > /dev/null 2>&1; then
    echo "  Starting Telegram bridge..."
    python3 src/telegram-bridge.py > src/telegram-bridge.log 2>&1 &
    echo "  ✓ telegram bridge"
  else
    echo "  ✓ telegram bridge (already running)"
  fi
else
  echo "  ~ telegram bridge (no token — optional)"
fi

# 7. Discord bridge (optional — needs DISCORD_BOT_TOKEN)
if [ -f "$HOME/.claude/channels/discord/.env" ] && grep -q "DISCORD_BOT_TOKEN=" "$HOME/.claude/channels/discord/.env" 2>/dev/null; then
  if ! python3 -c "import discord" 2>/dev/null; then
    echo "  ~ discord bridge (needs: pip3 install discord.py)"
  elif ! pgrep -f "discord-bridge" > /dev/null 2>&1; then
    echo "  Starting Discord bridge..."
    python3 src/discord-bridge.py > src/discord-bridge.log 2>&1 &
    echo "  ✓ discord bridge"
  else
    echo "  ✓ discord bridge (already running)"
  fi
else
  echo "  ~ discord bridge (no token — optional)"
fi

echo ""

# Verify services actually started (wait a moment, then check ports)
sleep 3
echo "Verifying services..."
for port_name in "9900:voice-agent" "8080:web-client" "7844:dashboard" "7843:agent-api" "7845:screen-capture"; do
  port="${port_name%%:*}"
  name="${port_name##*:}"
  if lsof -i :"$port" > /dev/null 2>&1; then
    echo "  ✓ $name (port $port)"
  else
    echo "  ✗ $name (port $port) — check src/${name}.log"
  fi
done
echo ""
open "http://localhost:8080"

# Check if a sutando-core session is already running
if pgrep -f "claude.*--name.*sutando-core" > /dev/null 2>&1; then
  echo "Claude Code (sutando-core) is already running."
  echo "To restart: kill it first, then re-run this script."
  echo ""
else
  echo "Starting Claude Code (sutando-core)..."
  echo ""
  exec claude --name sutando-core --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    -- "/proactive-loop"
fi

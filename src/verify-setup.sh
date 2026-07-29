#!/bin/bash
# Sutando setup verification — checks everything a new user needs
# Usage: bash src/verify-setup.sh

PASS=0
FAIL=0
WARN=0

pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ~ $1"; WARN=$((WARN+1)); }

echo "Sutando Setup Verification"
echo "========================================"

# 1. Prerequisites
echo ""
echo "Prerequisites:"

if command -v node &>/dev/null; then
  NODE_VER=$(node --version | sed 's/v//')
  NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1)
  if [ "$NODE_MAJOR" -ge 22 ]; then
    pass "Node.js $NODE_VER"
  else
    fail "Node.js $NODE_VER (need 22+)"
  fi
else
  fail "Node.js not found (brew install node)"
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE_RUNTIME="$(bash "$REPO/scripts/sutando-config.sh" core-runtime 2>/dev/null || echo claude)"
if command -v "$CORE_RUNTIME" &>/dev/null; then
  if [ "$CORE_RUNTIME" = "codex" ] && ! codex login status >/dev/null 2>&1; then
    fail "Codex CLI is not authenticated (run: codex login)"
  else
    pass "$CORE_RUNTIME core CLI"
  fi
else
  fail "$CORE_RUNTIME core CLI not found"
fi

if command -v fswatch &>/dev/null; then
  pass "fswatch"
else
  fail "fswatch not found (brew install fswatch)"
fi

if command -v python3 &>/dev/null; then
  pass "Python3 $(python3 --version 2>&1 | sed 's/Python //')"
else
  fail "Python3 not found"
fi

# 2. Configuration
echo ""
echo "Configuration:"

if [ -f .env ]; then
  if grep -q "^GEMINI_API_KEY=" .env && ! grep -q "^GEMINI_API_KEY=$" .env; then
    pass "GEMINI_API_KEY set"
  else
    warn "GEMINI_API_KEY not set in .env (optional — voice disabled)"
  fi
  if grep -q "^GMAIL_ADDRESS=" .env && ! grep -q "^#.*GMAIL_ADDRESS" .env; then
    pass "Gmail configured"
  else
    warn "Gmail not configured (optional — email features disabled)"
  fi
  if grep -q "^TWILIO_ACCOUNT_SID=" .env && ! grep -q "^#.*TWILIO_ACCOUNT_SID" .env; then
    pass "Twilio configured"
  else
    warn "Twilio not configured (optional — phone features disabled)"
  fi
  _CHAN_BASE="$(bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/sutando-config.sh" claude-home-path channels)"
  if [ -f "$_CHAN_BASE/telegram/.env" ]; then
    pass "Telegram bot configured"
  else
    warn "Telegram not configured (optional — run /telegram:configure)"
  fi
  if [ -f "$_CHAN_BASE/discord/.env" ]; then
    if python3 -c "import discord" 2>/dev/null; then
      pass "Discord bot configured"
    else
      warn "Discord configured but discord.py missing (pip3 install discord.py)"
    fi
  else
    warn "Discord not configured (optional — run /discord:configure)"
  fi
  if [ -f "$_CHAN_BASE/slack/.env" ]; then
    if python3 -c "import slack_bolt" 2>/dev/null; then
      pass "Slack bot configured"
    else
      warn "Slack configured but slack_bolt missing (pip3 install slack_bolt)"
    fi
  else
    warn "Slack not configured (optional — run /slack:configure)"
  fi
else
  warn ".env file missing (optional for text-only + core operation)"
fi

# 3. Dependencies
echo ""
echo "Dependencies:"

if [ -d node_modules ]; then
  pass "node_modules installed"
else
  fail "node_modules missing (run: npm install)"
fi

# 4. Key files
echo ""
echo "Key files:"

for f in src/voice-agent.ts src/task-bridge.ts src/web-client.ts src/health-check.py src/agent-api.py src/dashboard.py CLAUDE.md; do
  if [ -f "$f" ]; then
    pass "$f"
  else
    fail "$f missing"
  fi
done

# 5. Directories
echo ""
echo "Directories:"

for d in tasks results notes; do
  if [ -d "$d" ]; then
    pass "$d/"
  else
    mkdir -p "$d"
    pass "$d/ (created)"
  fi
done

# 6. Ports (if services are running)
echo ""
echo "Services (if running):"

for port_name in "9900:voice-agent" "8080:web-client" "7844:dashboard" "7843:agent-api"; do
  port="${port_name%%:*}"
  name="${port_name##*:}"
  if lsof -i :"$port" &>/dev/null; then
    pass "$name (port $port)"
  else
    warn "$name not running (port $port) — will start with startup.sh"
  fi
done

# 7. macOS permissions
echo ""
echo "macOS permissions:"

if sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db "SELECT client FROM access WHERE service='kTCCServiceScreenCapture'" 2>/dev/null | grep -qi "claude\|node\|terminal\|iterm\|warp"; then
  pass "Screen Recording permission granted"
else
  warn "Screen Recording — may need to grant in System Settings → Privacy"
fi

# Summary
echo ""
echo "========================================"
echo "  $PASS passed, $FAIL failed, $WARN warnings"
if [ "$FAIL" -eq 0 ]; then
  echo "  Ready to run: bash src/startup.sh"
else
  echo "  Fix the failures above, then run: bash src/startup.sh"
fi

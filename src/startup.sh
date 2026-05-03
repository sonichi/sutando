#!/bin/bash
# Sutando startup — starts all services + Claude Code.
# Usage: bash src/startup.sh

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Auto-bootstrap: create-if-missing files and dirs that the agent + skills
# expect to exist (logs, state, tasks, results, notes, contextual-chips.json,
# pending-questions.md, build_log.md, crons.json, …). Idempotent — safe to
# run on every start. Replaces the bare `mkdir -p logs state` that used to
# live here. See src/init.sh for the full list.
bash "$REPO/src/init.sh" --auto

echo "Sutando startup..."
echo ""

# Preflight summary line — what env / CLI / perms are missing. One line, no
# blocking; problems are surfaced but startup continues so the user can fix
# things piece by piece.
bash "$REPO/src/init.sh" --preflight | tail -1

# Install dependencies if needed
if [ ! -d node_modules ]; then
  if command -v npm > /dev/null 2>&1 && npm install 2>/dev/null; then
    echo "  ✓ Dependencies installed (npm)"
  elif command -v pnpm > /dev/null 2>&1 && pnpm install 2>/dev/null; then
    echo "  ✓ Dependencies installed (pnpm)"
  elif command -v yarn > /dev/null 2>&1 && yarn install 2>/dev/null; then
    echo "  ✓ Dependencies installed (yarn)"
  else
    echo "  ✗ Could not install dependencies."
    echo "    Try: curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash"
    echo "    Then: nvm install 24 && npm install"
    exit 1
  fi
fi

# Check prerequisites
missing=0
if ! command -v node > /dev/null 2>&1; then echo "  ✗ node not found — brew install node"; missing=1; fi
if ! command -v npx > /dev/null 2>&1; then echo "  ✗ npx not found — comes with node"; missing=1; fi
if ! command -v python3 > /dev/null 2>&1; then echo "  ✗ python3 not found"; missing=1; fi
if ! command -v claude > /dev/null 2>&1; then echo "  ✗ claude not found — see https://docs.anthropic.com/en/docs/claude-code/getting-started"; missing=1; fi
if ! command -v fswatch > /dev/null 2>&1; then
  if command -v brew > /dev/null 2>&1; then
    echo "  ⚠ fswatch not found — installing via Homebrew..."
    brew install fswatch
    if command -v fswatch > /dev/null 2>&1; then
      echo "  ✓ fswatch installed"
    else
      echo "  ✗ fswatch installation failed"; missing=1
    fi
  else
    echo "  ✗ fswatch not found — brew install fswatch"; missing=1
  fi
fi
if [ ! -f .env ]; then echo "  ✗ .env not found — cp .env.example .env and add your keys"; missing=1; fi
# Load .env and check required keys
if [ -f .env ]; then
  set -a; source .env; set +a
  if [ -z "$GEMINI_API_KEY" ]; then echo "  ✗ GEMINI_API_KEY not set in .env — get one at https://ai.google.dev"; missing=1; fi
fi
if [ $missing -eq 1 ]; then echo ""; echo "Fix the above and try again."; exit 1; fi

# Check macOS permissions (can't grant programmatically, just warn)
# Prevent display sleep (important for always-on Mac Mini — Zoom/summon fails on lock screen)
if ! pgrep -q caffeinate; then
  caffeinate -d -i -s &
  echo "  ✓ caffeinate started (prevents display sleep)"
else
  echo "  ✓ caffeinate already running"
fi

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
mkdir -p tasks results data

# Archive stale results/*.txt (>24h) BEFORE any service starts iterating
# results/. Prevents the 2026-04-15 DM-flood class of incidents where a
# freshly-restarted task-bridge or discord-bridge poll loop sees a backlog
# of long-dead result files and re-delivers them. Post-mortem:
# notes/post-mortem-dm-flood-2026-04-15.md.
python3 "$REPO/src/archive-stale-results.py" || true

# 0. Credential proxy for quota tracking (port 7846)
if ! lsof -i :7846 > /dev/null 2>&1; then
  echo "  Starting credential proxy (port 7846)..."
  npx tsx ~/.claude/skills/quota-tracker/scripts/credential-proxy.ts > /tmp/credential-proxy.log 2>&1 &
  sleep 1
  if lsof -i :7846 > /dev/null 2>&1; then
    echo "  ✓ credential proxy"
    export ANTHROPIC_BASE_URL=http://localhost:7846
  else
    echo "  ⚠ credential proxy failed — Claude will connect directly (check /tmp/credential-proxy.log)"
  fi
else
  echo "  ✓ credential proxy (already running)"
  export ANTHROPIC_BASE_URL=http://localhost:7846
fi

# 1. Voice agent (Gemini Live on port 9900)
if ! lsof -i :9900 > /dev/null 2>&1; then
  echo "  Starting voice agent (port 9900)..."
  npx tsx src/voice-agent.ts > logs/voice-agent.log 2>&1 &
  echo "  ✓ voice agent"
else
  echo "  ✓ voice agent (already running)"
fi

# 2. Web client (port 8080)
if ! lsof -i :8080 > /dev/null 2>&1; then
  echo "  Starting web client (port 8080)..."
  npx tsx src/web-client.ts > logs/web-client.log 2>&1 &
  echo "  ✓ web client"
else
  echo "  ✓ web client (already running)"
fi

# 3. Dashboard (port 7844)
if ! lsof -i :7844 > /dev/null 2>&1; then
  echo "  Starting dashboard (port 7844)..."
  python3 src/dashboard.py > logs/dashboard.log 2>&1 &
  echo "  ✓ dashboard"
else
  echo "  ✓ dashboard (already running)"
fi

# 4. Agent API (port 7843)
if ! lsof -i :7843 > /dev/null 2>&1; then
  echo "  Starting agent API (port 7843)..."
  python3 src/agent-api.py > logs/agent-api.log 2>&1 &
  echo "  ✓ agent API"
else
  echo "  ✓ agent API (already running)"
fi

# 5. Screen capture server (port 7845)
if ! lsof -i :7845 > /dev/null 2>&1; then
  echo "  Starting screen capture (port 7845)..."
  python3 src/screen-capture-server.py > logs/screen-capture.log 2>&1 &
  echo "  ✓ screen capture"
else
  echo "  ✓ screen capture (already running)"
fi

# 5b. Sutando context drop app (global hotkey ⌃C)
SUT_SRC="$REPO/src/Sutando/main.swift"
SUT_BIN="$REPO/src/Sutando/Sutando"

# Rebuild if source is newer than binary, or binary is missing.
# Kill any running instance so the fresh binary can take over.
if [ -f "$SUT_SRC" ] && { [ ! -f "$SUT_BIN" ] || [ "$SUT_SRC" -nt "$SUT_BIN" ]; }; then
  echo "  Compiling Sutando (source newer than binary)..."
  if (cd "$REPO/src/Sutando" && swiftc -O -o Sutando main.swift -framework Cocoa -framework Carbon -framework ApplicationServices -framework AVFoundation 2>/dev/null); then
    echo "  ✓ Sutando compiled"
    if pgrep -f "src/Sutando/Sutando" > /dev/null 2>&1; then
      pkill -f "src/Sutando/Sutando" 2>/dev/null || true
      # Wait for kernel cleanup to drain before relaunch — fixed sleep 1
      # raced with slow shutdown on 2026-04-21, leaving dual Sutando.app
      # instances with ghost menu-bar icons.
      for _ in $(seq 1 30); do
        pgrep -f "src/Sutando/Sutando" >/dev/null 2>&1 || break
        sleep 0.1
      done
    fi
  else
    echo "  ⚠ Sutando compile failed — keeping existing binary if any"
  fi
fi

if ! pgrep -f "src/Sutando/Sutando" > /dev/null 2>&1; then
  if [ -f "$SUT_BIN" ]; then
    echo "  Starting Sutando..."
    "$SUT_BIN" > /dev/null 2>&1 &
    echo "  ✓ Sutando (⌃C/⌃V/⌃M)"
  else
    echo "  ⚠ Sutando binary missing — hotkeys disabled"
  fi
else
  echo "  ✓ Sutando (already running)"
fi

echo ""

# 6. Telegram bridge (optional — needs TELEGRAM_BOT_TOKEN, skip with SKIP_TELEGRAM=1)
if [ "${SKIP_TELEGRAM:-}" = "1" ]; then
  echo "  ~ telegram bridge (skipped via SKIP_TELEGRAM)"
elif [ -f "$HOME/.claude/channels/telegram/.env" ] && grep -q "TELEGRAM_BOT_TOKEN=" "$HOME/.claude/channels/telegram/.env" 2>/dev/null; then
  if ! pgrep -f "telegram-bridge" > /dev/null 2>&1; then
    echo "  Starting Telegram bridge..."
    python3 src/telegram-bridge.py > logs/telegram-bridge.log 2>&1 &
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
    python3 src/discord-bridge.py > logs/discord-bridge.log 2>&1 &
    echo "  ✓ discord bridge"
  else
    echo "  ✓ discord bridge (already running)"
  fi
else
  echo "  ~ discord bridge (no token — optional)"
fi

# 8. Phone conversation server + ngrok (optional — needs Twilio creds, skip with SKIP_PHONE=1)
if [ "${SKIP_PHONE:-}" = "1" ]; then
  echo "  ~ conversation server (skipped via SKIP_PHONE)"
elif grep -q "TWILIO_ACCOUNT_SID=" .env 2>/dev/null; then
  if ! pgrep -f "conversation-server" > /dev/null 2>&1; then
    echo "  Starting conversation server..."
    npx tsx skills/phone-conversation/scripts/conversation-server.ts > /tmp/conversation-server.log 2>&1 &
    echo "  ✓ conversation server (port 3100)"
  else
    echo "  ✓ conversation server (already running)"
  fi
  if ! pgrep -f "ngrok" > /dev/null 2>&1; then
    echo "  Starting ngrok tunnel..."
    # If NGROK_DOMAIN is set in .env, use the reserved domain for a stable URL.
    # Otherwise ngrok picks a random subdomain and the Twilio webhook must be
    # updated manually on every restart.
    NGROK_DOMAIN_VAL=$(grep -E '^NGROK_DOMAIN=' .env 2>/dev/null | head -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'")
    if [ -n "$NGROK_DOMAIN_VAL" ]; then
      ngrok http 3100 --domain="$NGROK_DOMAIN_VAL" --log=stdout > /tmp/ngrok.log 2>&1 &
    else
      ngrok http 3100 --log=stdout > /tmp/ngrok.log 2>&1 &
    fi
    sleep 3
    NGROK_URL=$(curl -s http://localhost:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])" 2>/dev/null || echo "")
    if [ -n "$NGROK_URL" ]; then
      # Update WEBHOOK_BASE_URL in .env — portable in-place edit.
      # `sed -i ''` is BSD-only; on Macs with Homebrew gnu-sed in PATH it
      # silently fails (treats '' as an input filename). tmpfile + mv works
      # on both. See #412 cold-review for the coreutils-in-PATH context.
      if grep -q "WEBHOOK_BASE_URL=" .env; then
        tmpfile=$(mktemp)
        sed "s|WEBHOOK_BASE_URL=.*|WEBHOOK_BASE_URL=$NGROK_URL|" .env > "$tmpfile" && mv "$tmpfile" .env
      else
        echo "WEBHOOK_BASE_URL=$NGROK_URL" >> .env
      fi
      if [ -n "$NGROK_DOMAIN_VAL" ]; then
        echo "  ✓ ngrok ($NGROK_URL — reserved domain, no Twilio update needed)"
      else
        echo "  ✓ ngrok ($NGROK_URL)"
        echo "  ⚠ Update Twilio webhook to: $NGROK_URL"
      fi
    else
      echo "  ✗ ngrok (failed to start)"
    fi
  else
    echo "  ✓ ngrok (already running)"
  fi
else
  echo "  ~ conversation server (no Twilio creds — optional)"
fi

echo ""

# Verify services actually started (wait a moment, then check ports)
sleep 3
echo "Verifying services..."
VERIFY_PORTS="9900:voice-agent 8080:web-client 7844:dashboard 7843:agent-api 7845:screen-capture"
if [ "${SKIP_PHONE:-}" != "1" ] && grep -q "TWILIO_ACCOUNT_SID=" .env 2>/dev/null; then
  VERIFY_PORTS="$VERIFY_PORTS 3100:conversation-server"
fi
for port_name in $VERIFY_PORTS; do
  port="${port_name%%:*}"
  name="${port_name##*:}"
  if lsof -i :"$port" > /dev/null 2>&1; then
    echo "  ✓ $name (port $port)"
  else
    echo "  ✗ $name (port $port) — check logs/${name}.log"
  fi
done
echo ""
open "http://localhost:8080"

# Check if a sutando-core session is already running
if pgrep -f "claude.*--name.*sutando-core" > /dev/null 2>&1; then
  echo "Claude Code (sutando-core) is already running."
  # Auto-attach when invoked from an interactive terminal — saves the user
  # the copy-paste of the long `tmux -S /tmp/sutando-tmux.sock attach -t
  # sutando-core` command. Falls back to printing the instruction in
  # non-interactive contexts (launchd, cron, CI) where exec'ing into an
  # attach would hang without a tty.
  if [ -t 1 ] && command -v tmux > /dev/null 2>&1; then
    echo "Attaching... (Ctrl-b d to detach)"
    exec tmux -S /tmp/sutando-tmux.sock attach -t sutando-core
  fi
  echo "To restart: kill it first, then re-run this script."
  echo "To attach to the running session: tmux -S /tmp/sutando-tmux.sock attach -t sutando-core"
  echo ""
else
  echo "Starting Claude Code (sutando-core) inside tmux..."
  echo ""
  # Wrap in a tmux session named `sutando-core` so Sutando.app can send
  # keystrokes into the pane when the task watcher dies (see
  # AppDelegate.checkWatcher). `tmux new-session -A -s sutando-core …`
  # creates the session if it doesn't exist and attaches if it does,
  # so the user sees the Claude Code prompt exactly as before.
  # Auto-install tmux via Homebrew if missing. Sutando.app's
  # watcher-auto-restart (PR #444) depends on a tmux-wrapped CLI pane,
  # so a first-run user without tmux silently loses that feature.
  if ! command -v tmux > /dev/null 2>&1 && command -v brew > /dev/null 2>&1; then
    echo "tmux not found — installing via Homebrew (~30s, required for Sutando.app watcher-auto-restart)..."
    brew install tmux 2>&1 | tail -3
  fi
  # Fall back to a bare `exec claude` if tmux is still missing.
  if command -v tmux > /dev/null 2>&1; then
    # Explicit socket path so Sutando.app (which runs under a different
    # TMPDIR due to macOS sandboxing when launched via `open`) can reach
    # the same tmux server. Without -S, tmux defaults to
    # $TMPDIR/tmux-$(id -u)/default — different path app-side vs
    # shell-side → tmux has-session fails → watcher-auto-restart falls
    # back to macOS notification instead of sending `watcher`.
    exec tmux -S /tmp/sutando-tmux.sock new-session -A -s sutando-core \
      claude --name sutando-core --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
      -- "/proactive-loop"
  else
    echo "  ⚠ tmux not found — running without tmux wrapper"
    echo "    (Sutando.app's watcher-auto-restart won't work; brew install tmux to enable)"
    exec claude --name sutando-core --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
      -- "/proactive-loop"
  fi
fi

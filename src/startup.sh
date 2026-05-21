#!/bin/bash
# Sutando startup — starts all services + Claude Code.
# Usage: bash src/startup.sh

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

# Export workspace root so child processes (skills, gather scripts, etc.) can
# resolve "the Sutando workspace" without walking dirname-relative paths that
# break when the script is invoked via a userSettings hardlink. Picked up by
# skills/self-diagnose/scripts/gather.sh and any other script that honors
# $SUTANDO_ROOT.
export SUTANDO_ROOT="$REPO"

# Git committer attribution from stand-identity.json (opt-in, no env var).
# The repo-local `user.email` (GitHub privacy noreply) is the AUTHOR — CLA
# resolves it to the owner's account. The COMMITTER carries per-host
# attribution so `git log --format='%h %an / %cn %s'` shows which Sutando
# host crafted each commit. Silent fall-through on every "no identity" path
# (missing file, missing jq, empty fields); OSS users ship no identity file.
if [ -f "$REPO/stand-identity.json" ] && command -v jq > /dev/null 2>&1; then
  _stand_name=$(jq -r '.name // empty' "$REPO/stand-identity.json" 2>/dev/null)
  _stand_machine=$(jq -r '.machine // empty' "$REPO/stand-identity.json" 2>/dev/null)
  if [ -n "$_stand_name" ] && [ -n "$_stand_machine" ]; then
    git -C "$REPO" config committer.name "$_stand_name"
    git -C "$REPO" config committer.email "${_stand_machine}@noreply.sutando.local"
  fi
  unset _stand_name _stand_machine
fi

# Per-machine runtime state. Resolves $SUTANDO_WORKSPACE, defaulting to
# ~/.sutando/workspace/ (the canonical workspace per docs/workspace-design.md).
# The .app build may pin SUTANDO_WORKSPACE to another location if needed.
STATE_ROOT="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
LOGS_DIR="$STATE_ROOT/logs"
mkdir -p "$LOGS_DIR"

# Layered .env: load $SUTANDO_WORKSPACE/.env when present, so its values take
# precedence over the repo .env. Python services inherit these via the
# child process environment; Node services additionally re-load via
# src/load-env.ts at import time.
if [ -f "$STATE_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$STATE_ROOT/.env"
  set +a
fi

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
# macOS 15+ silently writes a tiny PNG when Screen Recording is denied (exit 0).
# Discriminator: real captures are hundreds-of-KB to MB; denied artifacts <2KB.
# An all-black 5120x2880 PNG compresses to ~43KB, so 5KB is the safe floor.
PERM_OK=1
screencapture -x /tmp/sutando-permcheck.png 2>/dev/null || PERM_OK=0
if [ "$PERM_OK" -eq 1 ]; then
  # wc -c is portable across BSD (macOS) and GNU coreutils.
  permcheck_size=$(wc -c < /tmp/sutando-permcheck.png 2>/dev/null | tr -d ' ' || echo 0)
  if [ "${permcheck_size:-0}" -lt 5000 ]; then PERM_OK=0; fi
fi
rm -f /tmp/sutando-permcheck.png
if [ "$PERM_OK" -eq 0 ]; then
  echo "  ⚠ Screen Recording not granted (or stale)"
  echo "    → System Settings → Privacy & Security → Screen & System Audio Recording"
  echo "    → Add the app running this terminal (Terminal.app / iTerm2 / Warp / VS Code / Cursor / etc.)"
  echo "    → Fully Quit the terminal app, then re-open. macOS caches the perm until process restart."
  if lsof -i :7845 > /dev/null 2>&1; then
    echo "    → A screen-capture server is already running on :7845 with the old (denied) perm."
    echo "      Kill it before re-running: lsof -ti:7845 | xargs kill"
  fi
else
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

# Create tasks/ and results/ directories under SUTANDO_HOME (or repo fallback)
mkdir -p "$STATE_ROOT/tasks" "$STATE_ROOT/results" "$STATE_ROOT/data"

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

# 1. Voice agent (Gemini Live on port 9900 + conversation HTTP on port 8080).
# PR-A folded the standalone web-client HTTP server into this process via
# src/web-server.ts, so one Node process owns both ports.
if ! lsof -i :9900 > /dev/null 2>&1; then
  echo "  Starting voice agent (ports 9900 + 8080)..."
  npx tsx src/voice-agent.ts > "$LOGS_DIR/voice-agent.log" 2>&1 &
  echo "  ✓ voice agent"
else
  echo "  ✓ voice agent (already running)"
fi

# 2. Dashboard (port 7844)
if ! lsof -i :7844 > /dev/null 2>&1; then
  echo "  Starting dashboard (port 7844)..."
  python3 src/dashboard.py > "$LOGS_DIR/dashboard.log" 2>&1 &
  echo "  ✓ dashboard"
else
  echo "  ✓ dashboard (already running)"
fi

# 3. Agent API (port 7843)
if ! lsof -i :7843 > /dev/null 2>&1; then
  echo "  Starting agent API (port 7843)..."
  python3 src/agent-api.py > "$LOGS_DIR/agent-api.log" 2>&1 &
  echo "  ✓ agent API"
else
  echo "  ✓ agent API (already running)"
fi

# 4. Screen capture server (port 7845)
# Skip when Screen Recording perm is missing — otherwise we'd start a server
# that returns black-PNG denials, the stale-7845 state the permcheck warns about.
if ! lsof -i :7845 > /dev/null 2>&1; then
  if [ "$PERM_OK" -eq 1 ]; then
    echo "  Starting screen capture (port 7845)..."
    python3 src/screen-capture-server.py > "$LOGS_DIR/screen-capture.log" 2>&1 &
    echo "  ✓ screen capture"
  else
    echo "  ⊘ screen capture skipped — grant Screen Recording perm first, then re-run startup.sh"
  fi
else
  echo "  ✓ screen capture (already running)"
fi

# 4a. Terminal server (port 7847) — bridges xterm.js in the unified UI
# CLI pane to the sutando-core tmux session.
if ! lsof -i :7847 > /dev/null 2>&1; then
  echo "  Starting terminal server (port 7847)..."
  npx tsx src/terminal-server.ts > "$LOGS_DIR/terminal-server.log" 2>&1 &
  echo "  ✓ terminal server"
else
  echo "  ✓ terminal server (already running)"
fi

# 5b. Sutando context drop app (global hotkey ⌃C)
SUT_SRC_DIR="$REPO/src/Sutando"
SUT_BIN="$SUT_SRC_DIR/Sutando"
SUT_SRC_FILES=("$SUT_SRC_DIR/main.swift" "$SUT_SRC_DIR/LaunchAgentInstaller.swift" "$SUT_SRC_DIR/SparkleUpdater.swift" "$SUT_SRC_DIR/CloudAuth.swift" "$SUT_SRC_DIR/CloudClient.swift" "$SUT_SRC_DIR/EnvFile.swift" "$SUT_SRC_DIR/FeedbackWindow.swift" "$SUT_SRC_DIR/OnboardingWindow.swift" "$SUT_SRC_DIR/Permissions.swift" "$SUT_SRC_DIR/ScreenCaptureSupervisor.swift" "$SUT_SRC_DIR/SettingsWindow.swift" "$SUT_SRC_DIR/UnifiedMainWindow.swift" "$SUT_SRC_DIR/Uninstaller.swift" "$SUT_SRC_DIR/WebWindow.swift")

# Rebuild if any source file is newer than binary, or binary is missing.
sut_needs_build=0
if [ ! -f "$SUT_BIN" ]; then
  sut_needs_build=1
else
  for f in "${SUT_SRC_FILES[@]}"; do
    if [ -f "$f" ] && [ "$f" -nt "$SUT_BIN" ]; then sut_needs_build=1; break; fi
  done
fi

# Kill any running instance so the fresh binary can take over.
if [ "$sut_needs_build" = "1" ]; then
  echo "  Compiling Sutando (source newer than binary)..."
  if (cd "$SUT_SRC_DIR" && swiftc -O -o Sutando "${SUT_SRC_FILES[@]##*/}" -framework Cocoa -framework Carbon -framework ApplicationServices -framework AVFoundation 2>/dev/null); then
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
    python3 src/telegram-bridge.py > "$LOGS_DIR/telegram-bridge.log" 2>&1 &
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
    python3 src/discord-bridge.py > "$LOGS_DIR/discord-bridge.log" 2>&1 &
    echo "  ✓ discord bridge"
  else
    echo "  ✓ discord bridge (already running)"
  fi
else
  echo "  ~ discord bridge (no token — optional)"
fi

# 7.5 Cloud memory backup daemon — uploads $SUTANDO_MEMORY_DIR + notes/ on
# a slow loop. No-op when signed out or on a free plan, so it's safe to
# always start. Disable with SKIP_MEMORY_BACKUP=1 (e.g. when iterating on
# the backup script itself).
if [ "${SKIP_MEMORY_BACKUP:-}" = "1" ]; then
  echo "  ~ memory backup loop (skipped via SKIP_MEMORY_BACKUP)"
elif ! pgrep -f "memory-backup-loop" > /dev/null 2>&1; then
  echo "  Starting memory backup loop..."
  bash src/memory-backup-loop.sh > "$LOGS_DIR/memory-backup.log" 2>&1 &
  echo "  ✓ memory backup loop (every ~20 min when signed-in + paid tier)"
else
  echo "  ✓ memory backup loop (already running)"
fi

# 7.6 Cloud-skill auto-installer loop — pulls user's installed-skill list
# and extracts any bundles that aren't on disk. Lets dashboard installs
# propagate to every Mac the user signs into. Disable with SKIP_SKILL_SYNC=1.
if [ "${SKIP_SKILL_SYNC:-}" = "1" ]; then
  echo "  ~ cloud-skill sync loop (skipped via SKIP_SKILL_SYNC)"
elif ! pgrep -f "cloud-skill-sync-loop" > /dev/null 2>&1; then
  echo "  Starting cloud-skill sync loop..."
  bash src/cloud-skill-sync-loop.sh > "$LOGS_DIR/cloud-skill-sync.log" 2>&1 &
  echo "  ✓ cloud-skill sync loop (every ~10 min)"
else
  echo "  ✓ cloud-skill sync loop (already running)"
fi

# 7.7 Superpower Station MCP registration — writes the Sutando MCP server
# entry into ~/.claude.json so Claude Code (in tmux core-agent or
# elsewhere) can talk to the gateway. No-op when signed out. Disable with
# SKIP_STATION_MCP=1.
if [ "${SKIP_STATION_MCP:-}" = "1" ]; then
  echo "  ~ station MCP register (skipped via SKIP_STATION_MCP)"
else
  python3 src/register-station-mcp.py 2>&1 | sed 's/^/  /' || true
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
VERIFY_PORTS="9900:voice-agent 8080:voice-agent-http 7844:dashboard 7843:agent-api 7845:screen-capture"
if [ "${SKIP_PHONE:-}" != "1" ] && grep -q "TWILIO_ACCOUNT_SID=" .env 2>/dev/null; then
  VERIFY_PORTS="$VERIFY_PORTS 3100:conversation-server"
fi
for port_name in $VERIFY_PORTS; do
  port="${port_name%%:*}"
  name="${port_name##*:}"
  if lsof -i :"$port" > /dev/null 2>&1; then
    echo "  ✓ $name (port $port)"
  else
    echo "  ✗ $name (port $port) — check $LOGS_DIR/${name}.log"
  fi
done
echo ""
open "http://localhost:8080"

# Delegate to scripts/start-cli.sh — canonical sutando-core launch command.
# Single source of truth so Sutando.app's Restart Core menu can invoke the
# same launch path without duplicating the tmux + claude flags.
exec bash "$REPO/scripts/start-cli.sh"

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

# Git committer attribution: REMOVED (2026-05-21). This block used to set
# committer.name/committer.email from stand-identity.json so `git log %cn`
# showed which fleet host crafted a commit. But git 2.31+ honors committer.*
# config natively, making the COMMITTER a non-GitHub identity
# (`<host>@noreply.sutando.local`) — and CLA-Assistant gates on BOTH the
# author AND the committer. Result: every fleet commit was CLA-blocked
# (PR #947 and others, 2026-05-21). Per-host attribution is not worth
# blocking every PR; if still wanted, carry it in a commit-message trailer
# (CLA-Assistant ignores trailers), never in the committer identity.
#
# Actively clear any stale committer.* a prior startup wrote, so every
# fleet host self-heals on its next boot.
git -C "$REPO" config --unset committer.name 2>/dev/null || true
git -C "$REPO" config --unset committer.email 2>/dev/null || true

# Fail-fast .env validation BEFORE init.sh. Two reasons must both hold:
#  1) init.sh resolves the workspace via `${SUTANDO_WORKSPACE/#~/$HOME}` with
#     fallback to `~/.sutando/workspace/`. If .env carries a SUTANDO_WORKSPACE=
#     override and we haven't sourced .env yet, init.sh seeds dirs and files
#     in the wrong location, leaving orphan ~/.sutando/workspace/ skeletons
#     on first-time installs (hosts without a separate .zshenv export).
#  2) If .env is missing or required keys are unset, the whole startup is
#     going to bail anyway — better to exit cleanly here than to run init.sh
#     + the dependency install + the perms checks first and then bail.
missing=0
if [ ! -f .env ]; then
  echo "  ✗ .env not found — cp .env.example .env and add your keys"
  missing=1
else
  set -a; source .env; set +a
  if [ -z "$GEMINI_API_KEY" ]; then
    echo "  ✗ GEMINI_API_KEY not set in .env — get one at https://ai.google.dev"
    missing=1
  fi
fi
if [ $missing -eq 1 ]; then echo ""; echo "Fix the above and try again."; exit 1; fi

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

# Check CLI prerequisites. (.env + required keys were already validated
# above before init.sh; node/npx/python3/claude/fswatch are checked here
# because they're not needed for init.sh's bootstrap step.)
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
# An all-black 5120x2880 PNG compresses to ~43KB (PNG handles flat colors well),
# so 5KB is the safe floor — well above any denied output, well below any real
# capture even on a locked / dark / blank desktop.
PERM_OK=1
screencapture -x /tmp/sutando-permcheck.png 2>/dev/null || PERM_OK=0
if [ "$PERM_OK" -eq 1 ]; then
  # wc -c is portable across BSD (macOS) and GNU coreutils (Homebrew may override).
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

# Resolve the runtime workspace (separate from $REPO so per-user state lives
# outside the git checkout). Same resolution shape as src/workspace_default.py:
#   1. $SUTANDO_WORKSPACE env override (tilde-expand if needed)
#   2. ~/.sutando/workspace/ (canonical default)
#
# All service logs + tasks/results land under $WORKSPACE/logs etc. instead of
# the repo-root legacy paths. health-check + dashboard already read from
# $WORKSPACE/logs — this redirects the writes to match.
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  WORKSPACE="${SUTANDO_WORKSPACE/#\~/$HOME}"
else
  WORKSPACE="$HOME/.sutando/workspace"
fi
mkdir -p "$WORKSPACE/logs" "$WORKSPACE/tasks" "$WORKSPACE/results" "$WORKSPACE/data" "$WORKSPACE/state"
LOGS_DIR="$WORKSPACE/logs"

# Reap any stale watch-tasks-stream watcher from a prior session. The
# in-session Stop hook (.claude/settings.json) handles clean shutdown, but
# a hard crash (SIGKILL, panic, force-quit, power loss) skips it and leaves
# an orphan fswatch process + stale PID file. On a fresh startup we kill
# the orphan (if the PID still names a live `watch-tasks-stream` process)
# and remove the PID file so the new session's watcher writes a fresh one.
# Skipping kills when the PID has been recycled by an unrelated process is
# important — `kill $PID` without the cmdline check would target whatever
# new program happens to hold the recycled PID.
WATCHER_PID_FILE="$WORKSPACE/state/watch-tasks-stream.pid"
if [ -f "$WATCHER_PID_FILE" ]; then
  STALE_PID="$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)"
  if [ -n "$STALE_PID" ] && ps -p "$STALE_PID" -o args= 2>/dev/null | grep -q "watch-tasks-stream"; then
    kill "$STALE_PID" 2>/dev/null || true
    echo "  ✓ reaped stale watch-tasks-stream watcher (pid $STALE_PID)"
  fi
  rm -f "$WATCHER_PID_FILE"
fi

# Legacy repo-root tasks/results/data still created for back-compat with
# scripts that haven't migrated yet; safe no-op once everything's switched.
mkdir -p tasks results data

# Archive stale results/*.txt (>24h) BEFORE any service starts iterating
# results/. Prevents the 2026-04-15 DM-flood class of incidents where a
# freshly-restarted task-bridge or discord-bridge poll loop sees a backlog
# of long-dead result files and re-delivers them. Post-mortem:
# notes/post-mortem-dm-flood-2026-04-15.md.
python3 "$REPO/src/archive-stale-results.py" || true

# Core heartbeat — per-host alive signal under state/cores/<hostname>.alive.
# Foundation for multi-core / cross-machine "who's running?" checks. Single
# instance per host; gracefully cleans up its .alive file on SIGTERM.
if ! pgrep -f "src/core_heartbeat.py" > /dev/null 2>&1; then
  echo "  Starting core heartbeat..."
  python3 "$REPO/src/core_heartbeat.py" > /tmp/core-heartbeat.log 2>&1 &
  echo "  ✓ core heartbeat"
else
  echo "  ✓ core heartbeat (already running)"
fi

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

# 0b. Obs collector (OPTIONAL — opt-in via SUTANDO_OBS_COLLECTOR=1).
# The single, source-agnostic local collector: it receives Claude Code hooks
# (and, later, voice / filewatcher / bridge events) on /ingest/<source>,
# normalizes them into the one event schema, and writes the durable JSONL floor
# at <workspace>/logs/events-*.jsonl (the visualizer tails that). Off by default
# — it's an observability/dev tool, not required for the agent to run.
#
# When enabled we also point the core's hooks at it (SUTANDO_OBS_ENDPOINT) UNLESS
# an endpoint is already set — e.g. a remote upstream collector — so the "always
# set hooks, only export when told where" contract still holds.
if [ "${SUTANDO_OBS_COLLECTOR:-}" = "1" ]; then
  OBS_PORT="${SUTANDO_OBS_PORT:-4000}"
  if ! lsof -i :"$OBS_PORT" > /dev/null 2>&1; then
    echo "  Starting obs collector (port $OBS_PORT)..."
    SUTANDO_WORKSPACE="$WORKSPACE" SUTANDO_OBS_PORT="$OBS_PORT" \
      npx tsx "$REPO/src/observability/boot.ts" > "$LOGS_DIR/collector.log" 2>&1 &
    echo "  ✓ obs collector"
  else
    echo "  ✓ obs collector (already running on $OBS_PORT)"
  fi
  # Wire the core's hooks to the local collector unless an endpoint is already set.
  if [ -z "${SUTANDO_OBS_ENDPOINT:-}" ]; then
    export SUTANDO_OBS_ENDPOINT="http://localhost:$OBS_PORT"
  fi
else
  echo "  ~ obs collector (disabled — set SUTANDO_OBS_COLLECTOR=1 to enable)"
fi
# A port can LISTEN while the service never responds (single-threaded server
# blocked on a silent connection, hung event loop). The lsof guards below only
# check LISTEN, so a wedged service is "already running" forever — exactly how
# the dashboard stayed unreachable for hours and voice-agent for 26h
# (2026-06-10). Probe with a real HTTP request before each guard; on timeout,
# kill the listener so the normal start path takes over. Any response byte
# counts as alive (a 404 or a WS handshake rejection is fine — curl exits 28
# only when nothing came back before the deadline). Probe path matches
# health-check.py's check_port: a cheap unknown path, NOT "/" (dashboard's "/"
# runs health-check.py as a subprocess — probing it from here would recurse).
reap_wedged_listener() {
  local port="$1" name="$2" rc=0
  lsof -i :"$port" -sTCP:LISTEN > /dev/null 2>&1 || return 0
  curl -s -o /dev/null -m 10 "http://127.0.0.1:$port/__liveness_probe__" || rc=$?
  if [ "$rc" -eq 28 ]; then
    echo "  ⚠ $name (port $port) listening but unresponsive — killing wedged listener"
    lsof -ti :"$port" -sTCP:LISTEN | xargs kill 2>/dev/null || true
    sleep 1
  fi
  return 0
}

# 1. Voice agent (Gemini Live on port 9900)
reap_wedged_listener 9900 voice-agent
if ! lsof -i :9900 > /dev/null 2>&1; then
  echo "  Starting voice agent (port 9900)..."
  npx tsx src/voice-agent.ts > "$LOGS_DIR/voice-agent.log" 2>&1 &
  echo "  ✓ voice agent"
else
  echo "  ✓ voice agent (already running)"
fi

# 2. Web client (port 8080)
reap_wedged_listener 8080 web-client
if ! lsof -i :8080 > /dev/null 2>&1; then
  echo "  Starting web client (port 8080)..."
  npx tsx src/web-client.ts > "$LOGS_DIR/web-client.log" 2>&1 &
  echo "  ✓ web client"
else
  echo "  ✓ web client (already running)"
fi

# 3. Dashboard (port 7844)
reap_wedged_listener 7844 dashboard
if ! lsof -i :7844 > /dev/null 2>&1; then
  echo "  Starting dashboard (port 7844)..."
  python3 src/dashboard.py > "$LOGS_DIR/dashboard.log" 2>&1 &
  echo "  ✓ dashboard"
else
  echo "  ✓ dashboard (already running)"
fi

# 4. Agent API (port 7843)
reap_wedged_listener 7843 agent-api
if ! lsof -i :7843 > /dev/null 2>&1; then
  echo "  Starting agent API (port 7843)..."
  python3 src/agent-api.py > "$LOGS_DIR/agent-api.log" 2>&1 &
  echo "  ✓ agent API"
else
  echo "  ✓ agent API (already running)"
fi

# 5. Screen capture server (port 7845)
# Skip when Screen Recording perm is missing — otherwise we'd start a server
# that returns black-PNG denials, which is exactly the stale-7845 state the
# permcheck above warns about.
reap_wedged_listener 7845 screen-capture
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

# 5a-bis. Portfolio + research dashboard (port 8899) — idempotent self-guard.
# Serves the research webapp with the live (read-only) portfolio panel and keeps
# its snapshot fresh via a background refresher daemon. No-op if not initialised.
if [ -d "$REPO/skills/portfolio-research" ]; then
  if [ ! -d "${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/research/portfolio/webapp" ]; then
    bash "$REPO/skills/portfolio-research/scripts/init-evergreen-webapp.sh" \
      > "$LOGS_DIR/portfolio-dashboard.log" 2>&1 || true
  fi
  bash "$REPO/skills/portfolio-research/scripts/serve-dashboard.sh" \
    >> "$LOGS_DIR/portfolio-dashboard.log" 2>&1 || true
  echo "  ✓ portfolio dashboard (port 8899)"
fi

# 5b. Sutando context drop app (global hotkey ⌃C)
SUT_SRC="$REPO/src/Sutando/main.swift"
SUT_BIN="$REPO/src/Sutando/Sutando"

# Build the public ax-read CLI if missing or older than any of its source
# files. Sutando.app's resolveAxReadPath() prefers private personal-deictic
# when installed; this public binary is the text-only fallback so public-repo
# users still get the ⌃C selection-drop experience.
#
# Staleness widened (per Mini's PR #907 review): trigger a rebuild when
# Package.swift / build.sh / any *.swift under Sources/ is newer than the
# binary, not just the main entry-point. Build failures are surfaced loudly
# (not >/dev/null 2>&1) — silent failure here was the exact regression class
# this skill is meant to prevent.
AXR_DIR="$REPO/skills/context-drop"
AXR_BIN="$AXR_DIR/ax-read"
AXR_NEWEST_SRC="$(find "$AXR_DIR/Sources" "$AXR_DIR/Package.swift" "$AXR_DIR/build.sh" -type f \( -name '*.swift' -o -name 'Package.swift' -o -name 'build.sh' \) 2>/dev/null | xargs -I{} stat -f '%m {}' {} 2>/dev/null | sort -rn | head -1 | awk '{print $2}')"
if [ -n "$AXR_NEWEST_SRC" ] && { [ ! -f "$AXR_BIN" ] || [ "$AXR_NEWEST_SRC" -nt "$AXR_BIN" ]; }; then
  echo "  Compiling public ax-read (skills/context-drop)..."
  if ! command -v swift >/dev/null 2>&1; then
    echo "  ⚠ ax-read build skipped: 'swift' not in PATH"
    echo "    → install Xcode Command Line Tools (xcode-select --install) for ⌃C selection drops on public-repo installs"
  elif (cd "$AXR_DIR" && bash build.sh); then
    echo "  ✓ ax-read built at $AXR_BIN"
  else
    echo "  ⚠ ax-read build FAILED — see Swift compiler output above"
    echo "    → Sutando.app will fall back to legacy in-process AX (broken for Electron under LSUIElement context)"
  fi
fi

# Rebuild if source is newer than binary, or binary is missing.
# Kill any running instance so the fresh binary can take over.
if [ -f "$SUT_SRC" ] && { [ ! -f "$SUT_BIN" ] || [ "$SUT_SRC" -nt "$SUT_BIN" ]; }; then
  echo "  Compiling Sutando (source newer than binary)..."
  if (cd "$REPO/src/Sutando" && swiftc -O -o Sutando main.swift -framework Cocoa -framework Carbon -framework ApplicationServices -framework AVFoundation 2>/dev/null); then
    echo "  ✓ Sutando compiled"

    # Sync the fresh binary into the .app bundle if one exists, ensure the
    # AppleEvents usage-description key is present, and re-sign so the
    # cdhash matches. Without NSAppleEventsUsageDescription macOS silently
    # denies AppleEvents — getFinderSelection() returns [] and the ⌃C
    # drop handler logs "Nothing selected" with no permission prompt.
    SUT_APP="$REPO/src/Sutando/Sutando.app"
    if [ -d "$SUT_APP" ]; then
      cp "$SUT_BIN" "$SUT_APP/Contents/MacOS/Sutando"
      /usr/libexec/PlistBuddy \
        -c "Add :NSAppleEventsUsageDescription string 'Sutando reads your Finder selection to drop files into the agent task queue.'" \
        "$SUT_APP/Contents/Info.plist" 2>/dev/null || true
      # Prefer a stable signing identity when one is installed so the TCC
      # Accessibility grant survives rebuilds (cdhash churn). Falls back to
      # ad-hoc when no such identity exists — public-repo users without a
      # personal signing cert get the same behavior as before.
      #
      # The designated requirement is identifier-only on purpose: the
      # grant binds to the bundle ID rather than cdhash, so a rebuild
      # against the same identity satisfies the requirement without
      # re-prompting. For installs without a cert, ad-hoc still requires
      # re-grant on each rebuild — same as the legacy behavior.
      SUT_SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | awk '/"Sutando Dev"/{print $2; exit}')"
      if [ -n "$SUT_SIGN_ID" ]; then
        codesign --force --sign "$SUT_SIGN_ID" --identifier com.sutando.menubar \
          --requirements '=designated => identifier "com.sutando.menubar"' \
          "$SUT_APP" 2>/dev/null || codesign --force --sign - "$SUT_APP" 2>/dev/null || true
        echo "  ✓ Sutando.app synced + signed (Sutando Dev + identifier-only DR)"
      else
        codesign --force --sign - "$SUT_APP" 2>/dev/null || true
        echo "  ✓ Sutando.app synced + signed (ad-hoc; install \"Sutando Dev\" cert for stable TCC)"
      fi
    fi

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
    # Pick an interpreter that can actually verify TLS. A cert-less framework
    # python (e.g. /Library/Frameworks/.../3.13 without certifi) resolves first
    # on some PATHs and then fails EVERY Telegram long-poll with
    # CERTIFICATE_VERIFY_FAILED — silently dropping all messages (cost us ~10h
    # on 2026-06-15, caught only by a stale-heartbeat health warning).
    _tg_tls_ok() { "$1" -c 'import urllib.request as u; u.urlopen("https://api.telegram.org",timeout=8)' >/dev/null 2>&1; }
    TGPY="python3"
    if ! _tg_tls_ok "$TGPY"; then
      for _c in "$(pyenv which python3 2>/dev/null)" python3.12 python3.11; do
        [ -n "$_c" ] && command -v "$_c" >/dev/null 2>&1 && _tg_tls_ok "$_c" && TGPY="$_c" && break
      done
    fi
    "$TGPY" src/telegram-bridge.py > "$LOGS_DIR/telegram-bridge.log" 2>&1 &
    echo "  ✓ telegram bridge ($TGPY)"
  else
    echo "  ✓ telegram bridge (already running)"
  fi
else
  echo "  ~ telegram bridge (no token — optional)"
fi

# AG2 remote relay client (optional channel — full docs + onboarding in
# skills/ag2-relay/). Silent unless AG2_REMOTE_TOKEN is set; to connect a new
# instance run:  bash skills/ag2-relay/onboard.sh
if [ -n "${AG2_REMOTE_TOKEN:-}" ] && [ -f skills/ag2-relay/remote-task-client.py ]; then
  if ! pgrep -f "remote-task-client" > /dev/null 2>&1; then
    python3 skills/ag2-relay/remote-task-client.py > "$LOGS_DIR/remote-task-client.log" 2>&1 &
    echo "  ✓ ag2 relay client"
  else
    echo "  ✓ ag2 relay client (already running)"
  fi
fi

# 7. Discord bridge (optional — needs DISCORD_BOT_TOKEN + discord.py)
#
# `python3` on $PATH is unpredictable across installs (miniconda, system,
# Homebrew). The bridge itself self-rescues by re-execing under a known-good
# interpreter (see top of src/discord-bridge.py), but launching it with the
# right one in the first place avoids the wasted process + traceback noise.
# Probe a fixed list of candidates in priority order; first one with discord.py
# wins. Same probe is also what's used in the bridge's rescue fallback.
if [ -f "$HOME/.claude/channels/discord/.env" ] && grep -q "DISCORD_BOT_TOKEN=" "$HOME/.claude/channels/discord/.env" 2>/dev/null; then
  PYTHON_WITH_DISCORD=""
  for _p in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$_p" >/dev/null 2>&1 && "$_p" -c "import discord" 2>/dev/null; then
      PYTHON_WITH_DISCORD="$_p"
      break
    fi
  done
  if [ -z "$PYTHON_WITH_DISCORD" ]; then
    echo "  ~ discord bridge (no python with discord.py — run: /opt/homebrew/bin/pip3 install discord.py)"
  elif ! pgrep -f "discord-bridge" > /dev/null 2>&1; then
    echo "  Starting Discord bridge with $PYTHON_WITH_DISCORD..."
    "$PYTHON_WITH_DISCORD" src/discord-bridge.py > "$LOGS_DIR/discord-bridge.log" 2>&1 &
    echo "  ✓ discord bridge"
  else
    echo "  ✓ discord bridge (already running)"
  fi
else
  echo "  ~ discord bridge (no token — optional)"
fi

# 7b. Slack bridge (optional — needs SLACK_BOT_TOKEN + SLACK_APP_TOKEN + slack_bolt)
# Probes the same Python-interpreter candidates as the discord bridge so a
# fresh-install miniconda env doesn't silently miss slack_bolt.
if [ -f "$HOME/.claude/channels/slack/.env" ] && grep -q "SLACK_BOT_TOKEN=" "$HOME/.claude/channels/slack/.env" 2>/dev/null; then
  PYTHON_WITH_SLACK=""
  for _p in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$_p" >/dev/null 2>&1 && "$_p" -c "import slack_bolt" 2>/dev/null; then
      PYTHON_WITH_SLACK="$_p"
      break
    fi
  done
  if [ -z "$PYTHON_WITH_SLACK" ]; then
    echo "  ~ slack bridge (no python with slack_bolt — run: /opt/homebrew/bin/pip3 install slack_bolt)"
  elif ! pgrep -f "slack-bridge" > /dev/null 2>&1; then
    echo "  Starting Slack bridge with $PYTHON_WITH_SLACK..."
    # Source the env file so SLACK_BOT_TOKEN / SLACK_APP_TOKEN reach the child.
    set -a; . "$HOME/.claude/channels/slack/.env"; set +a
    "$PYTHON_WITH_SLACK" src/slack-bridge.py > "$LOGS_DIR/slack-bridge.log" 2>&1 &
    echo "  ✓ slack bridge"
  else
    echo "  ✓ slack bridge (already running)"
  fi
else
  echo "  ~ slack bridge (no token — optional)"
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
if [ "${SUTANDO_OBS_COLLECTOR:-}" = "1" ]; then
  VERIFY_PORTS="$VERIFY_PORTS ${SUTANDO_OBS_PORT:-4000}:collector"
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

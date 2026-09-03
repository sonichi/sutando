#!/bin/bash
# Sutando restart — stops all background services, then restarts via startup.sh.
# Does NOT touch the Claude Code CLI (core agent) — that's managed separately.
# Usage: bash src/restart.sh
#   --stop-only    Stop without restarting

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# The sentinel IS the clean-exit signal, so a failed write must be visible: a
# stub interpreter here would let a stop look successful while nothing changed.
PY_BIN=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  PY_BIN="$(resolve_python "$REPO")"
fi
_shutdown_state() {
  if [ -z "$PY_BIN" ]; then
    echo "restart.sh: no runnable python3 — shutdown sentinel NOT $1" >&2
    return 1
  fi
  "$PY_BIN" "$REPO/src/shutdown.py" "$@" >/dev/null || {
    echo "restart.sh: shutdown.py $1 failed — sentinel state is NOT $1" >&2
    return 1
  }
}

echo "Stopping Sutando services..."
# Marked before killing so the intake gate holds new tasks while services stop.
# --stop-only leaves it set: that IS the core's clean-exit signal.
_shutdown_state mark "restart.sh${1:+ $1}" || true
# Voice-agent stop goes through the GUARDED lock takeover, never a broad
# `pkill -f voice-agent` (voice-reliability plan amendment U2): the old blind
# pkill could kill an unvalidated process and leave a live lock behind (or
# race a concurrent guarded acquisition). The whole validate → TERM → wait →
# KILL → revalidate → unlink transaction runs inside one voice-lock.py
# invocation under the fcntl guard; identity mismatch → takeover-blocked and
# nothing is signaled. Interpreter unavailable ⇒ fail closed (skip, warn) —
# never signal without validation.
if _VOICE_PY="$(bash "$REPO/scripts/sutando-config.sh" python-bin 2>/dev/null)"; then
    _VOICE_WS="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
    _VOICE_PIDFILE="$(bash "$REPO/scripts/sutando-config.sh" voice-pidfile "$_VOICE_WS" 2>/dev/null || true)"
    if [ -n "$_VOICE_WS" ] && [ -n "$_VOICE_PIDFILE" ] && [ -f "$_VOICE_PIDFILE" ]; then
        "$_VOICE_PY" "$REPO/scripts/voice-lock.py" takeover \
            --pidfile "$_VOICE_PIDFILE" \
            --guard "$_VOICE_WS/.voice-agent.lock.guard" \
            --workspace "$_VOICE_WS" \
            --mode adopted --port 9900 \
            --entry "$REPO/src/voice-agent.ts" \
            --entry "$REPO/dist/voice-agent.js" \
            || echo "  WARN voice-agent takeover blocked/failed — not killing blindly (lock left untouched)"
    fi
else
    echo "  WARN no usable python3 for the guarded voice lock helper — skipping voice-agent stop (fail closed)"
fi
pkill -f "web-client.ts" 2>/dev/null
pkill -f "dashboard.py" 2>/dev/null
pkill -f "agent-api.py" 2>/dev/null
pkill -f "screen-capture-server" 2>/dev/null
pkill -f "telegram-bridge" 2>/dev/null
pkill -f "discord-bridge" 2>/dev/null
pkill -f "slack-bridge" 2>/dev/null
pkill -f "remote-gateway-bridge" 2>/dev/null
# The deprecated `remote-relay-bridge.py` stub runpy-execs the gateway bridge
# IN-PROCESS, so its argv keeps the OLD filename while it runs the NEW code.
# `pkill -f remote-gateway-bridge` therefore cannot see it: measured on a peer
# host 2026-08-03, a stub-launched instance had been up 39 DAYS, survived every
# restart, and kept stamping tasks from 39-day-old code. Kill both names.
pkill -f "remote-relay-bridge" 2>/dev/null
pkill -f "observability/boot" 2>/dev/null
pkill -f "watch-tasks" 2>/dev/null
pkill -f "conversation-server" 2>/dev/null
pkill -f "ngrok" 2>/dev/null
# Credential proxy: handle the launchd-supervised job explicitly. pkill alone
# only bounces the worker — launchd's KeepAlive respawns it on its own throttle,
# so restart.sh wouldn't actually control the cycle. For a restart, kickstart -k;
# for --stop-only, bootout so KeepAlive doesn't resurrect it (startup.sh
# re-bootstraps it next start). Legacy bare-& launch (no job) falls back to pkill.
_PROXY_LABEL="com.sutando.credential-proxy"
_PROXY_SERVICE="gui/$(id -u)/$_PROXY_LABEL"
if launchctl print "$_PROXY_SERVICE" >/dev/null 2>&1; then
    if [ "$1" = "--stop-only" ]; then
        echo "  Stopping launchd-supervised credential proxy..."
        launchctl bootout "$_PROXY_SERVICE" 2>/dev/null || true
    else
        echo "  Restarting launchd-supervised credential proxy..."
        launchctl kickstart -k "$_PROXY_SERVICE" 2>/dev/null
        # Wait for an actual LISTENer, not just any socket on 7846 — a bare
        # `lsof -i :7846` also matches transient client connections and would
        # break out before the proxy has rebound.
        for _ in $(seq 1 20); do lsof -nP -iTCP:7846 -sTCP:LISTEN >/dev/null 2>&1 && break; sleep 0.25; done
    fi
else
    pkill -f "credential-proxy" 2>/dev/null
fi
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
    "screen-capture-server" "telegram-bridge" "discord-bridge" "slack-bridge"
    "remote-gateway-bridge" "remote-relay-bridge" "observability/boot" "watch-tasks"
    "conversation-server" "ngrok" "src/Sutando/Sutando"
)
for _ in $(seq 1 30); do
    still=0
    for pat in "${STOP_PATTERNS[@]}"; do
        if pgrep -f "$pat" >/dev/null 2>&1; then still=1; break; fi
    done
    [ $still -eq 0 ] && break
    sleep 0.1
done

# Restart, not a stop: the core is NOT in STOP_PATTERNS and survives this, so a
# A restart is not a shutdown: a sentinel left set would make the surviving
# core read it as one. --stop-only exits above and deliberately keeps it.
_shutdown_state clear || true
# Relaunch what line 73 killed. This belongs here, not in startup.sh: that file
# is guarded headless (tests/startup-headless.test.sh) and owns no desktop UI.
APP_BIN="$REPO/src/Sutando/Sutando"
if pgrep -x Sutando > /dev/null 2>&1; then
    echo "  ✓ Sutando.app (already running)"
elif [ -x "$APP_BIN" ]; then
    nohup "$APP_BIN" > /tmp/sutando-app.log 2>&1 &
    sleep 1
    # `pgrep -x`, never `-f`: -f matches this script's own argv and would report
    # a launch that did not happen. The ✓ stays inside the verified branch.
    if pgrep -x Sutando > /dev/null 2>&1; then
        echo "  ✓ Sutando.app relaunched"
    else
        echo "  ✗ Sutando.app — launched but not running; see /tmp/sutando-app.log"
    fi
else
    echo "  ⊘ Sutando.app skipped — no binary at $APP_BIN"
fi

echo "Starting..."
exec bash "$REPO/src/startup.sh"

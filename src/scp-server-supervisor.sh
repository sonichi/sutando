#!/bin/bash
# Supervise the SCP server (runtime-api): keep the device gateway alive for as
# long as this sutando host session lives, respawning it on crash.
#
# Why not launchd: this checkout lives under ~/Documents, which TCC denies to
# launchd background items — the job dies EX_CONFIG before exec (observed live
# 2026-08-12). A startup.sh child inherits the session's TCC grants, and the
# owner's lifecycle requirement is exactly "SCP server follows sutando" —
# active together, down together, respawned in between.
#
# The server owns its own mDNS advertisement (dns-sd child), so respawning the
# server re-advertises automatically and a crash withdraws the name instead of
# promising a dead port.
#
# Started by startup.sh 5a (idempotent via pidfile); usable standalone:
#   bash src/scp-server-supervisor.sh &        # supervise until killed
#
# Env: expects the caller to have sourced .env (startup.sh does); re-sources
# it per spawn so flag edits take effect on the next respawn, not next boot.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/src/workspace_resolve.sh"
resolve_workspace_or_die

PIDFILE="$WORKSPACE/state/scp-server-supervisor.pid"
LOG="$WORKSPACE/logs/runtime-api.log"
mkdir -p "$WORKSPACE/state" "$WORKSPACE/logs"

# Single instance: a live supervisor keeps its pidfile fresh; a stale pidfile
# (dead pid) is taken over. kill -0 not pgrep — argv matching self-matches.
if [ -f "$PIDFILE" ]; then
  oldpid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    echo "scp-server-supervisor: already running (pid $oldpid)"
    exit 0
  fi
fi
echo $$ > "$PIDFILE"

PY="$(command -v python3 || true)"
[ -x /opt/homebrew/bin/python3 ] && PY=/opt/homebrew/bin/python3

server_pid=""
cleanup() {
  [ -n "$server_pid" ] && kill "$server_pid" 2>/dev/null
  rm -f "$PIDFILE"
  exit 0
}
trap cleanup TERM INT

while :; do
  if ! lsof -nP -iTCP:"${SUTANDO_SCP_WSS_PORT:-8787}" -sTCP:LISTEN >/dev/null 2>&1; then
    (
      cd "$REPO" || exit 1
      set -a; . ./.env 2>/dev/null; set +a
      exec "$PY" src/runtime-api/server.py
    ) >> "$LOG" 2>&1 &
    server_pid=$!
    echo "scp-server-supervisor: (re)started server pid $server_pid" >> "$LOG"
  fi
  sleep 10
done

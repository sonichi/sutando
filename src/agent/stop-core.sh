#!/bin/bash
# src/agent/stop-core.sh — stop ONLY the core CLI tmux session (sonichi#2401).
#
# The core-only counterpart to `start-cli.sh --restart`: kills the
# sutando-core session and nothing else (bridges, dashboard, proxy all keep
# running — that separation is what made chat-triggered restart possible in
# the 2026-07-29 outage, and "Stop All Services" already exists for the rest).
#
# Stop means stop (owner decision on #2401): nothing anywhere relaunches a
# stopped core automatically. Idempotent — exits 0 when no session exists.
#
# Socket + session resolution match start-cli.sh: $SUTANDO_TMUX_SOCKET /
# $SUTANDO_TMUX_SESSION, else the defaults. Selectors use tmux exact-match
# (=name) so a similarly-prefixed session (e.g. sutando-core-debug) is never
# probe-matched or killed (john-the-dev review, #2408).

set -e

SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"

if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
  echo "stop-core: no $SESSION session on $TMUX_SOCKET — nothing to stop"
  exit 0
fi

# Kill the watcher sibling first if present (same cleanup start-cli.sh does on
# --restart), then the core session itself.
# Publish the durable stop tombstone BEFORE killing sessions, and only on the
# path that actually stops one — the no-session early exit above must NOT write
# it, or a crashed core probed by stop-core would be masked from recovery.
# Best-effort: stopping must never fail because the tombstone could not land.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/../core_heartbeat.py" --mark-stopped \
  || echo "stop-core: warning — could not write stop tombstone (recover-core may treat this as a crash)" >&2

tmux -S "$TMUX_SOCKET" kill-session -t "=${SESSION}-watcher" 2>/dev/null || true
tmux -S "$TMUX_SOCKET" kill-session -t "=$SESSION"
echo "stop-core: $SESSION stopped (socket $TMUX_SOCKET)"

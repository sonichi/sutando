#!/bin/bash
# pool-status.sh — one-shot live snapshot of the multi-worker agent pool (#880).
#
# Complements scripts/pool-metrics.py: that one is a historic latency rollup,
# this one is a "what's the pool doing right now" five-section view. Read-only.
#
# Sections:
#   1. launchd state           — which plists are loaded + their PIDs/exit codes
#   2. liveness                — per-slot .alive heartbeats with age in seconds
#   3. channel-affinity        — which slot owns which channel right now
#   4. recent claims           — last N tasks that were atomically claimed
#   5. log tails               — last K lines from each worker's stdout log
#
# Usage:
#   bash scripts/pool-status.sh              # defaults: 5 claims, 3 log lines
#   bash scripts/pool-status.sh 20 10        # 20 claims, 10 log lines
#
# Exit code: 0 always (read-only diagnostic — no failure surface).

set -u

CLAIMS_LIMIT="${1:-5}"
LOG_LINES="${2:-3}"

WORKSPACE="$(bash "$(dirname "$0")/sutando-config.sh" workspace)"
WORKSPACE="${WORKSPACE/#\~/$HOME}"
CORES_DIR="$WORKSPACE/state/cores"
LOG_DIR="$WORKSPACE/logs"
TASKS_DIR="$WORKSPACE/tasks"

# Header helper — visually separates sections without depending on color.
hdr() { printf "\n=== %s ===\n" "$1"; }

# ---------------------------------------------------------------------------
hdr "1. launchd state"
# Filter to com.sutando.worker-N labels (legacy core-N too). The trailing column is PID; "-" means
# not currently running (KeepAlive will respawn). Numeric column is exit code
# of the last instance — 127 = wrapper script missing (see issue #886).
if launchctl list 2>/dev/null | awk 'NR==1 || /com\.sutando\.(worker|core)-[0-9]/' \
     | awk '{ printf "%-10s %-8s %s\n", $1, $2, $3 }'; then :; fi

# ---------------------------------------------------------------------------
hdr "2. liveness (.alive heartbeats)"
now="$(date +%s)"
if [ -d "$CORES_DIR" ]; then
  shopt -s nullglob
  found=0
  for f in "$CORES_DIR"/worker-*.alive "$CORES_DIR"/core-*.alive; do
    found=1
    base="$(basename "$f")"
    mtime="$(stat -f %m "$f" 2>/dev/null || echo 0)"
    age=$(( now - mtime ))
    status="alive"
    if [ "$age" -gt 90 ]; then status="STALE (>90s)"; fi
    printf "  %-22s age=%ds  %s\n" "$base" "$age" "$status"
  done
  shopt -u nullglob
  [ "$found" -eq 0 ] && echo "  (no .alive files — pool not running or heartbeats absent)"
else
  echo "  (state/cores/ missing — workspace=$WORKSPACE)"
fi

# ---------------------------------------------------------------------------
hdr "3. channel-affinity bindings"
if [ -d "$CORES_DIR" ]; then
  shopt -s nullglob
  found=0
  for f in "$CORES_DIR"/channel-*.handler; do
    found=1
    base="$(basename "$f")"
    body="$(cat "$f" 2>/dev/null || echo '?')"
    printf "  %s  %s\n" "$base" "$body"
  done
  shopt -u nullglob
  [ "$found" -eq 0 ] && echo "  (no handler bindings yet — affinity is lazy; binds on first claim)"
fi

# ---------------------------------------------------------------------------
hdr "4. recent claims (last $CLAIMS_LIMIT)"
# Claim filenames look like task-<ts>.claimed-worker-<N>.txt and land first in
# tasks/, then get archived to tasks/processed/ once the result lands. Scan
# both directories for full history.
{
  shopt -s nullglob
  for d in "$TASKS_DIR" "$TASKS_DIR/processed"; do
    for f in "$d"/task-*.claimed-*.txt; do
      mtime="$(stat -f %m "$f" 2>/dev/null || echo 0)"
      echo "$mtime $(basename "$f")"
    done
  done
  shopt -u nullglob
} | sort -rn | head -n "$CLAIMS_LIMIT" | awk '
  { ts=$1; $1=""; sub(/^ /, ""); printf "  %s  %s\n", strftime("%H:%M:%S", ts), $0 }
'
# If the loop produced nothing the head pipe is silent — emit a hint.
have_claims="$(find "$TASKS_DIR" "$TASKS_DIR/processed" -maxdepth 1 -name 'task-*.claimed-*.txt' 2>/dev/null | head -1)"
[ -z "$have_claims" ] && echo "  (no claimed-by-pool tasks yet)"

# ---------------------------------------------------------------------------
hdr "5. log tails (last $LOG_LINES lines, per slot)"
shopt -s nullglob
for log in "$LOG_DIR"/worker-[0-9]*.log "$LOG_DIR"/core-[0-9]*.log; do
  base="$(basename "$log")"
  printf -- "--- %s ---\n" "$base"
  tail -n "$LOG_LINES" "$log" 2>/dev/null | sed 's/^/  /'
done
shopt -u nullglob

echo

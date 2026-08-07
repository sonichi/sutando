#!/bin/bash
# Graceful-restart Phase-1 prep; see notes/graceful-restart-design.md.
# ALWAYS terminates and ALWAYS writes a terminal ready/failed sentinel.
set -uo pipefail

RID="${1:?usage: restart-prep.sh <restart_id>}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WS="${GR_WS:-$(bash "$REPO/scripts/sutando-config.sh" workspace)}"   # GR_WS: test-only workspace override
READY="$WS/state/restart-ready.json"
FAILED="$WS/state/restart-prep-failed.json"
STEP_TIMEOUT=45      # per-step bound so prep can't hang; falls back to background timeout if `timeout` is absent (macOS)

log() { echo "restart-prep[$RID]: $*"; }

fail() {
  local reason="$1"
  log "FAILED: $reason"
  printf '{"restart_id":"%s","ts":%s,"reason":"%s"}\n' "$RID" "$(date +%s)" "$reason" > "$FAILED"
  exit 3
}

# macOS has no `timeout` binary; emulate a bounded run so a step can't hang the prep.
bounded() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; return $?; fi
  "$@" & local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) & local killer=$!
  wait "$pid"; local rc=$?
  kill -TERM "$killer" 2>/dev/null; wait "$killer" 2>/dev/null || true
  return $rc
}

log "Phase-1 starting…"

# Step 1 — checkpoint: record the current in-flight picture (informational; orphan-recovery is the backstop).
queue_n="$(ls "$WS/tasks/"task-*.txt 2>/dev/null | grep -v "task-restart-prep-" | wc -l | tr -d ' ')"
log "checkpoint: $queue_n non-prep task(s) in queue"

# Step 2 — sync workspace before the kill. Bounded. Production passes explicit
# argv so a space-containing checkout path stays ONE argument.
sync_ok=0
if [ -n "${GR_SYNC_CMD:-}" ]; then                                # GR_SYNC_CMD: test-only override
  if bounded "$STEP_TIMEOUT" bash -c "$GR_SYNC_CMD" >/dev/null 2>&1; then sync_ok=1; fi
else
  if bounded "$STEP_TIMEOUT" bash "$REPO/scripts/sync-workspace.sh" >/dev/null 2>&1; then sync_ok=1; fi
fi
if [ "$sync_ok" = 1 ]; then
  synced=true
else
  # A sync failure is a real signal — surface it, don't silently proceed to a kill.
  fail "sync-workspace failed or timed out (${STEP_TIMEOUT}s)"
fi

# Step 3 — record branch + dirty-tree so the next session knows the state.
branch="$(git -C "$REPO" branch --show-current 2>/dev/null || echo unknown)"
dirty="$(git -C "$REPO" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

# Terminal success sentinel.
printf '{"restart_id":"%s","ts":%s,"synced":%s,"branch":"%s","dirty":%s,"queue":%s}\n' \
  "$RID" "$(date +%s)" "$synced" "$branch" "$dirty" "$queue_n" > "$READY"
log "READY: synced=$synced branch=$branch dirty=$dirty queue=$queue_n"

#!/bin/bash
# Graceful-restart Phase-1 prep; see notes/graceful-restart-design.md.
# ALWAYS terminates and ALWAYS writes a terminal ready/failed sentinel.
set -uo pipefail

RID="${1:?usage: restart-prep.sh <restart_id>}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WS="${GR_WS:-$(bash "$REPO/scripts/sutando-config.sh" workspace)}"   # GR_WS: test-only workspace override
READY="$WS/state/restart-ready.json"
FAILED="$WS/state/restart-prep-failed.json"
STEP_TIMEOUT="${GR_STEP_TIMEOUT:-45}"   # per-step bound so prep can't hang; falls back to a background timeout if `timeout` is absent (macOS). GR_STEP_TIMEOUT: test-only override

log() { echo "restart-prep[$RID]: $*"; }

fail() {
  local reason="$1"
  log "FAILED: $reason"
  printf '{"restart_id":"%s","ts":%s,"reason":"%s"}\n' "$RID" "$(date +%s)" "$reason" > "$FAILED"
  exit 3
}

# macOS has no `timeout`; signals must reach the child's process GROUP, because a sync
# that spawns a helper outlives a pid-only kill and keeps writing to the workspace.
bounded() {
  local secs="$1"; shift
  local grace="${GR_KILL_GRACE_S:-3}"
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after="$grace" "$secs" "$@"; return $?
  fi
  # `set -m` makes the job a process-group leader, so pgid == pid and `-$pid` is the
  # group. Without it the job shares OUR group and `-$pid` would signal this shell.
  set -m; "$@" & local pid=$!; set +m
  ( sleep "$secs"; kill -TERM "-$pid" 2>/dev/null
    sleep "$grace"; kill -KILL "-$pid" 2>/dev/null ) & local killer=$!
  wait "$pid"; local rc=$?
  kill -TERM "$killer" 2>/dev/null; wait "$killer" 2>/dev/null || true
  # The group can outlive its leader; drain it before the caller reports a verdict.
  if kill -0 "-$pid" 2>/dev/null; then
    kill -KILL "-$pid" 2>/dev/null
    local waited=0
    while kill -0 "-$pid" 2>/dev/null && [ "$waited" -lt 20 ]; do sleep 0.1; waited=$((waited+1)); done
  fi
  return $rc
}

log "Phase-1 starting…"

# Step 1 — checkpoint: record the current in-flight picture (informational; orphan-recovery is the backstop).
queue_n="$(ls "$WS/tasks/"task-*.txt 2>/dev/null | grep -v "task-restart-prep-" | wc -l | tr -d ' ')"
log "checkpoint: $queue_n non-prep task(s) in queue"

# Step 2 — sync workspace before the kill. Bounded. Production passes explicit
# argv so a space-containing checkout path stays ONE argument.
sync_ok=0
sync_rc=0
sync_log="$(mktemp "${TMPDIR:-/tmp}/gr-sync.XXXXXX")"
if [ -n "${GR_SYNC_CMD:-}" ]; then                                # GR_SYNC_CMD: test-only override
  bounded "$STEP_TIMEOUT" bash -c "$GR_SYNC_CMD" >"$sync_log" 2>&1 && sync_ok=1 || sync_rc=$?
else
  bounded "$STEP_TIMEOUT" bash "$REPO/scripts/sync-workspace.sh" >"$sync_log" 2>&1 && sync_ok=1 || sync_rc=$?
fi
if [ "$sync_ok" = 1 ]; then
  synced=true
  rm -f "$sync_log"
else
  # 124 GNU timeout, 143 TERM, 137 KILL — a TERM-ignoring child only dies at 137. A
  # pre-kill path must not report an immediate nonzero exit as if it hung for the bound.
  case "$sync_rc" in
    124|137|143) sync_why="timed out after ${STEP_TIMEOUT}s" ;;
    *)       sync_why="exited $sync_rc" ;;
  esac
  sync_tail="$(tail -c 400 "$sync_log" 2>/dev/null | tr '\n\t' '  ' | tr -s ' ' | sed 's/"/\\"/g')"
  rm -f "$sync_log"
  # A sync failure is a real signal — surface it, don't silently proceed to a kill.
  fail "sync-workspace $sync_why — last output: ${sync_tail:-(none)}"
fi

# Step 3 — record branch + dirty-tree so the next session knows the state.
branch="$(git -C "$REPO" branch --show-current 2>/dev/null || echo unknown)"
dirty="$(git -C "$REPO" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

# Terminal success sentinel.
printf '{"restart_id":"%s","ts":%s,"synced":%s,"branch":"%s","dirty":%s,"queue":%s}\n' \
  "$RID" "$(date +%s)" "$synced" "$branch" "$dirty" "$queue_n" > "$READY"
log "READY: synced=$synced branch=$branch dirty=$dirty queue=$queue_n"

#!/bin/bash
# Graceful core-restart orchestrator (design: notes/graceful-restart-design.md).
#
# Two-phase handshake that lets pre-restart work run BEFORE the kill:
#   1. Write a `task-restart-prep-<id>` task → the running agent picks it up and
#      runs Phase-1 (checkpoint + sync-workspace + record), then writes a
#      terminal sentinel: state/restart-ready.json (success) or
#      state/restart-prep-failed.json (a step failed).
#   2. This orchestrator WAITS ON EVENTS (never a wall-clock kill timer):
#        ready       -> exec start-cli.sh --restart          (clean)
#        prep-failed -> surface + exit 3, do NOT kill        (owner decides)
#        agent dead  -> abrupt restart (only safe fallback)  (no live work to lose)
#      A *busy but healthy* agent is waited for indefinitely (fresh heartbeat);
#      only genuine death (state/cores/<host>.alive stale > STALE_S) falls back
#      to abrupt. There is no "give up on a healthy agent" timer.
#
# The kill is owned HERE (external to the core) — the agent can never safely
# --restart itself (kill-session terminates it mid-task).
#
# Usage:
#   graceful-restart.sh              # real graceful restart
#   graceful-restart.sh --dry-run    # run the WHOLE handshake but SKIP the kill
#                                     # (test the machinery without ending the session)
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WS="${GR_WS:-$(bash "$REPO/scripts/sutando-config.sh" workspace)}"   # GR_WS: test-only workspace override
HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label)"

RID="grp-$(date +%s)-$$"                       # restart-id: scopes every artifact to THIS run
PREP_TASK="$WS/tasks/task-restart-prep-$RID.txt"
READY="$WS/state/restart-ready.json"
FAILED="$WS/state/restart-prep-failed.json"
ALIVE="$WS/state/cores/$HOST.alive"
STALE_S=90                                     # matches core_heartbeat's documented liveness threshold
POLL_S=2

log() { echo "graceful-restart[$RID]: $*"; }

# Clear any sentinels from a PRIOR restart so a stale file can't be mistaken for ours.
rm -f "$READY" "$FAILED"

mkdir -p "$WS/tasks"
cat > "$PREP_TASK" <<EOF
id: task-restart-prep-$RID
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
source: system
access_tier: owner
priority: urgent
task: RESTART_PREP restart_id=$RID — run graceful-restart Phase-1 by invoking: bash src/agent/restart-prep.sh $RID  (checkpoint in-flight, sync-workspace, record branch/state; writes state/restart-ready.json on success or state/restart-prep-failed.json on failure). Do NOT restart — the orchestrator owns the kill.
EOF
log "wrote prep task ($PREP_TASK); waiting for agent to signal (ready|failed|dead)…"

alive_age() {
  [ -f "$ALIVE" ] || { echo 999999; return; }
  echo "$(( $(date +%s) - $(stat -f %m "$ALIVE" 2>/dev/null || echo 0) ))"
}

do_restart() {
  local reason="$1"
  if [ "$DRY_RUN" = 1 ]; then
    log "DRY-RUN — would exec 'start-cli.sh --restart' now ($reason). Skipping the actual kill."
    return 0
  fi
  log "restarting core ($reason)…"
  exec bash "$REPO/src/agent/start-cli.sh" --restart
}

while true; do
  if [ -f "$READY" ] && grep -q "$RID" "$READY" 2>/dev/null; then
    log "prep READY: $(cat "$READY")"
    do_restart "prep-ready"
    exit 0
  fi
  if [ -f "$FAILED" ] && grep -q "$RID" "$FAILED" 2>/dev/null; then
    log "prep FAILED: $(cat "$FAILED") — NOT restarting; owner decides (fix+retry or force)."
    exit 3
  fi
  if [ "$(alive_age)" -gt "$STALE_S" ]; then
    log "agent DEAD (.alive stale > ${STALE_S}s) — sentinel can never come; abrupt fallback is safe (orphan-recovery catches queued tasks)."
    do_restart "agent-dead-abrupt"
    exit 0
  fi
  sleep "$POLL_S"
done

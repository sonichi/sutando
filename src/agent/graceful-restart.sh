#!/bin/bash
# Graceful core-restart orchestrator (design: notes/graceful-restart-design.md).
#
# Fully deterministic — no LLM/task-queue handoff anywhere. (An earlier
# revision wrote a RESTART_PREP task for the running agent to pick up;
# review sonichi#2334 flagged that a busy-or-wedged core could heartbeat
# forever without ever running prep, so the ready/failed sentinel might
# never appear. Prep is mechanical — sync-workspace + record state — so the
# orchestrator now invokes it DIRECTLY and gates the kill on explicit
# liveness/busyness signals instead of model discretion.)
#
#   Phase 1 — QUIET GATE (wait for a safe kill window):
#       dead   (.alive stale > STALE_S)  -> stop waiting; prep best-effort,
#              then restart (queued tasks survive in tasks/; orphan-recovery
#              is the backstop)
#       busy   (core-status.json = "running" AND its ts is fresh) -> wait;
#              there is NO give-up timer on a busy-but-healthy core
#       quiet  (anything else)           -> proceed
#     A "running" status whose ts is older than STATUS_TTL_S does NOT count
#     as busy — a wedged core that stopped updating its status must not hold
#     off the restart forever (the review's exact scenario). A malformed
#     status file (no parseable ts) also does not count as busy: the writer
#     contract always includes ts, and deterministic progress beats waiting
#     forever on a file nothing owns.
#   Phase 2 — PREP, invoked directly (self-bounded; see restart-prep.sh):
#       state/restart-ready.json        -> proceed
#       state/restart-prep-failed.json  -> surface + exit 3, do NOT kill
#     If the core is dead, prep failure does not block the restart (nothing
#     in-flight to lose; the sync was still attempted).
#   Phase 3 — exec start-cli.sh --restart. The kill is owned HERE (external
#     to the core) — the agent can never safely --restart itself
#     (kill-session terminates it mid-task).
#
# Usage:
#   graceful-restart.sh              # real graceful restart
#   graceful-restart.sh --dry-run    # run the WHOLE flow but SKIP the kill
#                                     # (test the machinery without ending the session)
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
WS="${GR_WS:-$(bash "$REPO/scripts/sutando-config.sh" workspace)}"   # GR_WS: test-only workspace override
HOST="$(bash "$REPO/scripts/sutando-config.sh" host-label)"

RID="grp-$(date +%s)-$$"                       # restart-id: scopes every artifact to THIS run
READY="$WS/state/restart-ready.json"
FAILED="$WS/state/restart-prep-failed.json"
ALIVE="$WS/state/cores/$HOST.alive"
STATUS="$WS/state/core-status.json"
STALE_S=90                                     # matches core_heartbeat's documented liveness threshold
STATUS_TTL_S="${GR_STATUS_TTL_S:-900}"         # "running" older than this = wedged, not busy (test override)
POLL_S="${GR_POLL_S:-2}"                       # test override

log() { echo "graceful-restart[$RID]: $*"; }

# Clear any sentinels from a PRIOR restart so a stale file can't be mistaken for ours.
rm -f "$READY" "$FAILED"

alive_age() {
  [ -f "$ALIVE" ] || { echo 999999; return; }
  echo "$(( $(date +%s) - $(stat -f %m "$ALIVE" 2>/dev/null || echo 0) ))"
}

# Busy = core-status.json claims "running" AND its self-reported ts is fresh.
busy() {
  [ -f "$STATUS" ] || return 1
  grep -q '"status"[[:space:]]*:[[:space:]]*"running"' "$STATUS" 2>/dev/null || return 1
  local ts
  ts="$(grep -o '"ts"[[:space:]]*:[[:space:]]*[0-9][0-9]*' "$STATUS" 2>/dev/null | grep -o '[0-9][0-9]*$' || true)"
  [ -n "$ts" ] || return 1
  [ "$(( $(date +%s) - ts ))" -le "$STATUS_TTL_S" ]
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

# ---- Phase 1: quiet gate -------------------------------------------------
DEAD=0
if [ "$(alive_age)" -gt "$STALE_S" ]; then
  DEAD=1
  log "core is DEAD (.alive stale/absent > ${STALE_S}s) — no wait; prep runs best-effort"
else
  log "quiet gate: waiting for a safe kill window (busy = core-status running + fresh ts)…"
  i=0
  while busy; do
    if [ "$(alive_age)" -gt "$STALE_S" ]; then
      DEAD=1
      log "core died while waiting — proceeding (prep best-effort)"
      break
    fi
    i=$((i + 1))
    [ $((i % 15)) -eq 0 ] && log "still busy — waiting (no give-up timer on a healthy core)…"
    sleep "$POLL_S"
  done
fi

# ---- Phase 2: prep, direct invocation ------------------------------------
log "running prep (direct invocation — no task-queue handoff)…"
prep_rc=0
bash "$REPO/src/agent/restart-prep.sh" "$RID" || prep_rc=$?

# ---- Phase 3: decide -----------------------------------------------------
if [ -f "$READY" ] && grep -q "$RID" "$READY" 2>/dev/null; then
  log "prep READY: $(cat "$READY")"
  do_restart "prep-ready"
  exit 0
fi
if [ "$DEAD" = 1 ]; then
  log "prep produced no ready sentinel (rc=$prep_rc) but core is dead — restarting anyway (nothing in-flight to lose)"
  do_restart "agent-dead-abrupt"
  exit 0
fi
log "prep FAILED (rc=$prep_rc): $(cat "$FAILED" 2>/dev/null || echo 'no sentinel') — NOT restarting; owner decides (fix+retry or force)."
exit 3

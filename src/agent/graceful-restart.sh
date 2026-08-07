#!/bin/bash
# Graceful core-restart orchestrator (design: notes/graceful-restart-design.md).
#
#   Phase 1 — QUIET GATE:
#       dead   (.alive stale > STALE_S)  -> prep best-effort, then restart
#       busy   (core-status "running" + fresh ts) -> wait, no give-up timer
#       quiet  (anything else)           -> proceed
#     A "running" status older than STATUS_TTL_S is wedged, not busy, and must
#     not hold the restart off forever; same for a status with no parseable ts.
#   Phase 2 — PREP, invoked directly (self-bounded; see restart-prep.sh):
#       state/restart-ready.json        -> proceed
#       state/restart-prep-failed.json  -> surface + exit 3, do NOT kill
#     A dead core's prep failure does not block the restart.
#   Phase 3 — exec start-cli.sh --restart. The kill is owned HERE, external to
#     the core: an agent cannot --restart itself without dying mid-task.
#
# Exit: 0 restarted · 3 prep failed (core untouched) · 4 deferred to a peer.
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

# ---- Phase 0: serialize -------------------------------------------------
# The sentinels are per-WORKSPACE and every run clears them, so without a lock
# two orchestrators each see a sentinel carrying their own rid and both restart.
# `mkdir` is the atomic primitive (fails if the path exists; unlike flock it is
# on macOS). Taken BEFORE clearing sentinels and held through the decision, so
# check and act cannot be split by a peer.
LOCKDIR="$WS/state/locks/graceful-restart.lock"
LOCK_STALE_S="${GR_LOCK_STALE_S:-900}"          # test override; a real run is seconds
mkdir -p "$WS/state/locks"

# A holder that died before its trap ran would wedge restarts forever, so an
# old lock is reapable — same liveness reasoning as workspace_lock.py, which
# keys on freshness rather than pid (pids recycle; a hung holder should lose it).
mtime_of() {  # epoch mtime, or EMPTY if no variant yields a numeric answer
  # `-f %m` is BSD, `-c %Y` GNU — but GNU's `-f` is --file-system and EXITS 0
  # with a dump, so select on all-digit OUTPUT, never on exit status.
  # Callers pick opposite fail-safes: lock age unreadable -> LIVE (never kill
  # blind); .alive age unreadable -> STALE (never let a dead core hide).
  local _ts
  for _fmt in "-f %m" "-c %Y"; do
    # _fmt is two intentional argv words, so it must stay unquoted.
    # shellcheck disable=SC2086
    _ts="$(stat $_fmt "$1" 2>/dev/null || true)"
    case "$_ts" in
      ''|*[!0-9]*) ;;                       # not a bare epoch — try the next variant
      *) printf '%s' "$_ts"; return 0 ;;
    esac
  done
  printf ''
}

if ! mkdir "$LOCKDIR" 2>/dev/null; then
  # Age MUST come from the directory's own mtime, which mkdir sets atomically
  # with the claim. A separate ts file written after mkdir reopens the race.
  held_ts="$(mtime_of "$LOCKDIR")"
  if [ -z "$held_ts" ]; then
    # Unreadable age -> assume LIVE and defer: fail closed, since the
    # alternative is a spurious destructive kill.
    log "another restart is in progress (age unreadable) — deferring, NOT restarting"
    exit 4
  fi
  age=$(( $(date +%s) - held_ts ))
  if [ "$age" -gt "$LOCK_STALE_S" ]; then
    log "reaping stale restart lock (age ${age}s > ${LOCK_STALE_S}s, holder $(cat "$LOCKDIR/rid" 2>/dev/null || echo '?'))"
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" 2>/dev/null || { log "lost the race to reap — another restart is in progress; deferring"; exit 4; }
  else
    log "another restart is in progress (holder $(cat "$LOCKDIR/rid" 2>/dev/null || echo '?'), age ${age}s) — deferring, NOT restarting"
    exit 4
  fi
fi
printf '%s' "$RID" > "$LOCKDIR/rid"
# Release only OUR lock. Guarded by the rid so a reaper's lock is never removed.
RESTART_DECIDED=0
cleanup_lock() {
  # A production restart ends in `exec`, so this trap never runs and the lock
  # outlives the process structurally — that is what stops a peer from killing
  # the core we just relaunched. A dry-run DOES reach here, and retaining would
  # self-block the operator's next real run for LOCK_STALE_S.
  if [ "$RESTART_DECIDED" = 1 ] && \
     { [ "$DRY_RUN" != 1 ] || [ "${GR_RETAIN_LOCK_ON_DECISION:-0}" = 1 ]; }; then
    return 0
  fi
  own_lock && rm -rf "$LOCKDIR"
  # Always succeed: a lock already gone or owned by a reaper is normal, and this
  # status would otherwise override the caller's exit code from an EXIT/TERM trap.
  return 0
}

# A stall past LOCK_STALE_S lets a peer reap our lock, after which $LOCKDIR is
# the REAPER's: renewing or acting on it then restarts alongside them.
own_lock() {
  [ "$(cat "$LOCKDIR/rid" 2>/dev/null || echo '')" = "$RID" ]
}

# A signal handler MUST exit explicitly: cleanup_lock alone returns and bash
# resumes the quiet-gate loop with the lock already released. 128+signo.
trap cleanup_lock EXIT
trap 'cleanup_lock; exit 130' INT
trap 'cleanup_lock; exit 143' TERM

# Clear any sentinels from a PRIOR restart so a stale file can't be mistaken for ours.
# Safe under the lock above: no peer can be mid-decision while we do this.
rm -f "$READY" "$FAILED"

alive_age() {
  [ -f "$ALIVE" ] || { echo 999999; return; }
  local _ts; _ts="$(mtime_of "$ALIVE")"
  # Unreadable mtime -> report STALE, same as a missing heartbeat. Opposite of
  # the lock's fail-closed: a dead core with a fresh core-status.json must not
  # be able to hide behind an unreadable .alive and stay in the busy wait.
  [ -n "$_ts" ] || { echo 999999; return; }
  echo "$(( $(date +%s) - _ts ))"
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
    # RENEW: this wait is unbounded but mkdir stamps $LOCKDIR's mtime ONCE, so an
    # unrenewed holder looks abandoned and a peer reaps it mid-wait. Touch the
    # DIRECTORY — the reaper reads that clock, not a sibling ts file. Only while
    # we still own it: renewing a foreign lock walks us into a concurrent restart.
    if ! own_lock; then
      log "lost the restart lease while waiting (holder is now $(cat "$LOCKDIR/rid" 2>/dev/null || echo 'gone')) — deferring, NOT restarting"
      exit 4
    fi
    touch "$LOCKDIR" 2>/dev/null || true
    sleep "$POLL_S"
  done
fi

# Re-check ownership after the gate and before anything destructive. The wait
# may have ended by the core going idle rather than by a renewal tick, so the
# loop's check is not sufficient on its own.
if ! own_lock; then
  log "lost the restart lease before prep (holder is now $(cat "$LOCKDIR/rid" 2>/dev/null || echo 'gone')) — deferring, NOT restarting"
  exit 4
fi

# ---- Phase 2: prep, direct invocation ------------------------------------
log "running prep (direct invocation — no task-queue handoff)…"
prep_rc=0
bash "$REPO/src/agent/restart-prep.sh" "$RID" || prep_rc=$?

# ---- Phase 3: decide -----------------------------------------------------
# Read the sentinel ONCE and decide from that snapshot: grep-then-cat is two
# reads of a shared path, so the logged body may not be the one validated.
ready_body="$(cat "$READY" 2>/dev/null || true)"
case "$ready_body" in
  *"$RID"*)
    log "prep READY: $ready_body"
    RESTART_DECIDED=1
    do_restart "prep-ready"
    exit 0
    ;;
esac
if [ "$DEAD" = 1 ]; then
  log "prep produced no ready sentinel (rc=$prep_rc) but core is dead — restarting anyway (nothing in-flight to lose)"
  RESTART_DECIDED=1
  do_restart "agent-dead-abrupt"
  exit 0
fi
log "prep FAILED (rc=$prep_rc): $(cat "$FAILED" 2>/dev/null || echo 'no sentinel') — NOT restarting; owner decides (fix+retry or force)."
exit 3

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

# ---- Phase 0: serialize -------------------------------------------------
# `restart-ready.json` / `restart-prep-failed.json` are per-WORKSPACE, not
# per-run, and every run clears them below. Two concurrent orchestrators can
# therefore each observe a sentinel carrying their OWN rid and both proceed to
# the destructive restart — the second kills the core the first just relaunched.
# Reproduced on this workspace at 30/30 two-process dry-run iterations.
#
# `mkdir` is the atomic primitive here: POSIX guarantees it fails if the path
# exists, and unlike `flock` it is present on macOS. The lock is taken BEFORE
# the sentinels are cleared and held through the terminal decision, so the
# check and the act cannot be split by a peer.
LOCKDIR="$WS/state/locks/graceful-restart.lock"
LOCK_STALE_S="${GR_LOCK_STALE_S:-900}"          # test override; a real run is seconds
mkdir -p "$WS/state/locks"

# A holder that died before its trap ran would wedge restarts forever, so an
# old lock is reapable — same liveness reasoning as workspace_lock.py, which
# keys on freshness rather than pid (pids recycle; a hung holder should lose it).
mtime_of() {  # epoch mtime, or EMPTY if no variant yields a numeric answer
  # `stat -f %m` is BSD/macOS; `stat -c %Y` is GNU. The trap is that GNU's `-f`
  # means --file-system: it prints a multi-line dump and EXITS 0, so a
  # `stat -f ... || stat -c ...` chain never reaches the fallback on Linux.
  # Selecting on EXIT STATUS is therefore wrong; select on the OUTPUT being
  # all-digits and try the next variant otherwise.
  # Callers choose their OWN fail-safe direction — they are opposite here:
  #   lock age   unreadable -> assume LIVE and defer (never kill blind)
  #   .alive age unreadable -> assume STALE (never let a dead core hide)
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
  # Age comes from the DIRECTORY's own mtime, which `mkdir` sets atomically as
  # part of the same operation that claims the lock. An earlier revision of this
  # guard wrote a `ts` file *after* mkdir and read that instead — reintroducing
  # the very race being fixed: a peer arriving in the gap read no ts, computed
  # `age = now - 0` (a full epoch), declared a one-second-old lock stale, reaped
  # it, and both runs restarted. Caught by the 30-iteration probe below.
  # Age via mtime_of (portable + digit-guarded); see its note for why.
  held_ts="$(mtime_of "$LOCKDIR")"
  if [ -z "$held_ts" ]; then
    # Cannot read the holder's age -> assume LIVE and defer. Failing closed is
    # the only safe default when the alternative is a spurious destructive kill.
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
  # A run that DECIDED to restart keeps the lock. In production `do_restart`
  # ends in `exec`, so this trap never fires and the lock outlives the process
  # by construction — that is precisely what stops a concurrent peer from
  # killing the core we just relaunched. Dry-run must model the same thing or
  # the concurrency test passes for a reason production does not share.
  # The lock is not permanent: the stale-reap above frees it, and the relaunched
  # core is expected to be serving long before LOCK_STALE_S elapses.
  [ "$RESTART_DECIDED" = 1 ] && return 0
  own_lock && rm -rf "$LOCKDIR"
  # ALWAYS succeed. Cleanup is best-effort: the lock being already gone, or
  # belonging to a reaper, is a normal outcome — not this run's exit status.
  #
  # Without this the function's status is `own_lock && rm -rf`, which is FALSE
  # whenever the lock isn't ours, and that leaks into the caller two ways
  # (qingyun-wu, #2334; both reproduced before fixing):
  #   - EXIT trap: bash takes the trap's last status, so the documented
  #     `exit 4` defer became 1.
  #   - TERM trap: `cleanup_lock; exit 143` under `set -e` aborts at
  #     cleanup_lock, so `exit 143` is NEVER REACHED and the shell exits 1.
  # Both are exactly the paths a supervisor branches on.
  return 0
}

# Do we still hold the lease we took? The lock directory can be reaped out from
# under a live holder (a scheduler stall past LOCK_STALE_S is enough), after
# which $LOCKDIR belongs to the REAPER. Renewing or acting on it then is worse
# than not renewing at all: we would keep the peer's lock fresh and proceed to
# restart alongside it — the double restart this lock exists to prevent
# (qingyun-wu reproduced it on #2334 by SIGSTOPping the holder past the
# threshold, letting a peer reap, then resuming: both runs restarted).
own_lock() {
  [ "$(cat "$LOCKDIR/rid" 2>/dev/null || echo '')" = "$RID" ]
}

# EXIT alone is not enough on a signal: `cleanup_lock` returns, and bash then
# RESUMES the interrupted command — the quiet-gate loop keeps running with the
# lock already released. Signal handlers must terminate explicitly. 128+signo
# preserves the conventional exit codes.
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
    # RENEW THE LOCK. The wait above is deliberately unbounded on a healthy core,
    # but the lock's staleness is judged from $LOCKDIR's own mtime, which `mkdir`
    # stamps ONCE at acquisition. Without this touch, a holder that waits longer
    # than LOCK_STALE_S looks abandoned to a second orchestrator, which reaps the
    # live holder's lock and enters the restart decision concurrently — two
    # destructive restarts, which is the exact thing this lock exists to prevent
    # (qingyun-wu, #2334). Renewing keeps "lock is fresh" meaning "holder is
    # alive", which is what the age check already assumes.
    #
    # Touching the DIRECTORY (not a file inside it) is deliberate: the reaper
    # reads the directory's mtime, so the renewal must move the same clock the
    # reaper reads. Writing a sibling `ts` file is what an earlier revision did,
    # and it reintroduced the race documented at the top of this file.
    # ...but ONLY while we still own it. Renewing a lock we no longer hold keeps
    # the REAPER's lock fresh and then walks us into a concurrent restart.
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
# Read the sentinel ONCE and decide from that snapshot. The previous form
# grepped the file and then `cat`-ed it again to log; those are two reads of a
# shared path, and the reviewer's evidence for the race was exactly a run whose
# grep matched its own rid while the following `cat` printed a peer's. Holding
# the lock already prevents a concurrent writer, but a single read means the
# logged body is provably the one that was validated.
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

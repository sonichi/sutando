#!/bin/bash
# Graceful core-restart orchestrator. Flow, flags and rationale live in
# notes/graceful-restart-design.md. Exit: 0 ok · 3 prep failed · 4 deferred ·
# 5 dry-run (nothing killed or restarted).
set -euo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && { DRY_RUN=1; shift; }
# Args after `--` reach start-cli.sh. The menu-bar restart needs --visible to
# survive the handoff, or its relaunch silently becomes detached.
[ "${1:-}" = "--" ] && shift
RESTART_ARGS=("$@")
# Log-only form: "${arr[@]}" inside a larger quoted string splits across log()'s
# parameters and is correct only by accident of `$*`.
RESTART_ARGS_STR=""
[ "${#RESTART_ARGS[@]}" -gt 0 ] && RESTART_ARGS_STR=" ${RESTART_ARGS[*]}"

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
STATUS_REREADS="${GR_STATUS_REREADS:-5}"       # empty-read retries before treating the status as absent
POLL_S="${GR_POLL_S:-2}"                       # test override

# The app pipes stdout only into itself, so a kill-without-relaunch left no
# trace on disk. Same text both ways: main.swift matches phases on its wording.
GR_LOG="$WS/logs/graceful-restart.log"
mkdir -p "$WS/logs" 2>/dev/null || true
log() {
  local line="graceful-restart[$RID]: $*"
  echo "$line"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >> "$GR_LOG" 2>/dev/null || true
}

# ---- Phase 0: serialize -------------------------------------------------

# Sentinels are per-WORKSPACE and every run clears them, so without a lock two
# orchestrators both restart. Held from before that clear through the decision.
LOCKDIR="$WS/state/locks/graceful-restart.lock"
LOCK_STALE_S="${GR_LOCK_STALE_S:-900}"          # test override; a real run is seconds
mkdir -p "$WS/state/locks"

# An old lock is reapable: freshness, not pid, is the liveness signal (pids
# recycle, and a holder that died before its trap would wedge restarts forever).
mtime_of() {  # epoch mtime, or EMPTY if no variant yields a numeric answer
  # GNU's `-f` is --file-system and EXITS 0 with a dump, so select on all-digit
  # OUTPUT, never exit status. Callers pick opposite fail-safes on EMPTY.
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
  # Age MUST come from the directory's own mtime (mkdir sets it atomically with
  # the claim); a ts file written after mkdir reopens the race.
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
  # Production ends in `exec` so this never runs and retention is structural.
  # A dry-run DOES reach here; retaining would self-block the next real run.
  if [ "$RESTART_DECIDED" = 1 ] && \
     { [ "$DRY_RUN" != 1 ] || [ "${GR_RETAIN_LOCK_ON_DECISION:-0}" = 1 ]; }; then
    return 0
  fi
  own_lock && rm -rf "$LOCKDIR"
  # Always succeed, or this status overrides the caller's exit code via the trap.
  return 0
}

# A stall past LOCK_STALE_S lets a peer reap our lock, after which $LOCKDIR is
# the REAPER's: renewing or acting on it then restarts alongside them.
own_lock() {
  [ "$(cat "$LOCKDIR/rid" 2>/dev/null || echo '')" = "$RID" ]
}

# A handler MUST exit explicitly: cleanup_lock alone returns and bash resumes
# the gate loop with the lock released. 128+signo.
trap cleanup_lock EXIT
trap 'cleanup_lock; exit 130' INT
trap 'cleanup_lock; exit 143' TERM

# Clear any sentinels from a PRIOR restart so a stale file can't be mistaken for ours.
# Safe under the lock above: no peer can be mid-decision while we do this.
rm -f "$READY" "$FAILED"

alive_age() {
  [ -f "$ALIVE" ] || { echo 999999; return; }
  local _ts; _ts="$(mtime_of "$ALIVE")"
  # Unreadable -> STALE (opposite of the lock's fail-closed): a dead core must
  # not hide behind an unreadable .alive and stay in the busy wait.
  [ -n "$_ts" ] || { echo 999999; return; }
  echo "$(( $(date +%s) - _ts ))"
}

# Busy = core-status.json claims "running" AND its self-reported ts is fresh.
busy() {
  [ -f "$STATUS" ] || return 1
  # Every writer is a `>` truncate-then-write, so a read can land on an EMPTY
  # file. Empty is unknown, not idle — re-read instead of authorising the kill.
  local raw="" i=0
  while :; do
    raw="$(cat "$STATUS" 2>/dev/null || true)"
    [ -n "$raw" ] && break
    i=$((i + 1))
    [ "$i" -ge "$STATUS_REREADS" ] && return 1
    sleep 0.05
  done
  printf '%s' "$raw" | grep -q '"status"[[:space:]]*:[[:space:]]*"running"' || return 1
  local ts
  ts="$(printf '%s' "$raw" | grep -o '"ts"[[:space:]]*:[[:space:]]*[0-9][0-9]*' \
        | grep -o '[0-9][0-9]*$' || true)"
  [ -n "$ts" ] || return 1
  [ "$(( $(date +%s) - ts ))" -le "$STATUS_TTL_S" ]
}

do_restart() {
  local reason="$1"
  if [ "$DRY_RUN" = 1 ]; then
    # Echo the REAL argv: without it the passthrough is unobservable in dry-run.
    log "DRY-RUN — would exec 'start-cli.sh --restart${RESTART_ARGS_STR}' now ($reason). Skipping the actual kill."
    # Exit rather than return: both call sites follow with `exit 0`, which is
    # indistinguishable from a real restart to anyone reading the status.
    exit 5
  fi
  log "restarting core ($reason)… — start-cli.sh's own trace continues in logs/restart-attempts.log"
  # The `+` guard keeps an empty array valid under `set -u` on bash 3.2.
  # GR_START_CLI is a test seam: dry-run never reaches this line.
  export GR_RID="$RID"   # lets the launcher release THIS run's lock if it aborts
  exec bash "${GR_START_CLI:-$REPO/src/agent/start-cli.sh}" --restart ${RESTART_ARGS[@]+"${RESTART_ARGS[@]}"}
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
    # RENEW the DIRECTORY's mtime (the clock the reaper reads): mkdir stamps it
    # once, so an unrenewed holder gets reaped mid-wait. Only while we own it.
    if ! own_lock; then
      log "lost the restart lease while waiting (holder is now $(cat "$LOCKDIR/rid" 2>/dev/null || echo 'gone')) — deferring, NOT restarting"
      exit 4
    fi
    touch "$LOCKDIR" 2>/dev/null || true
    sleep "$POLL_S"
  done
fi

# Re-check ownership after the gate: the wait can end via the core going idle
# rather than a renewal tick, so the loop's own check is not sufficient.
if ! own_lock; then
  log "lost the restart lease before prep (holder is now $(cat "$LOCKDIR/rid" 2>/dev/null || echo 'gone')) — deferring, NOT restarting"
  exit 4
fi

# ---- Phase 2: prep, direct invocation ------------------------------------
log "running prep (direct invocation — no task-queue handoff)…"
prep_rc=0
bash "$REPO/src/agent/restart-prep.sh" "$RID" || prep_rc=$?

# ---- Phase 3: decide -----------------------------------------------------

# Read the sentinel ONCE: grep-then-cat is two reads, so the logged body may not
# be the one validated.
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

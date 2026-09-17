#!/bin/bash
# Watcher ownership, shell half — the ONE sequence restart.sh and the startup
# reaper both run before signalling a pid, and the ONE stop that follows it.
# The record, checkout and executed-slot questions are answered by
# src/watcher_identity.py; the age question by src/watcher_sentinel.sh; every
# process read and signal goes through src/process-ops.sh so a test can replace
# that layer whole. Nothing here decides ownership on its own.
#
# watcher_confirm_owner <sentinel> <instance> <workspace> <code_path>
#   rc 0: WATCHER_OWNER_PID names the watcher and WATCHER_OWNER_INCARNATION the
#   start the record confirmed. rc 1: WATCHER_OWNER_REASON says which check
#   refused. Via variables, not stdout: `$( )` would lose them.
#   <code_path> is THIS checkout's src/watch-tasks-stream.sh — a record that
#   names another checkout's is refused even when its argv agrees with it.
# watcher_stop_owned <sentinel> <pid> [<incarnation>]
#   TERM, then wait for the exit; the sentinel is released only once the exit is
#   confirmed, and only while it still names <pid> under <incarnation> (default:
#   the one watcher_confirm_owner just confirmed) — a successor's record, even
#   one wearing the same pid, stays. rc 0 stopped, 1 the signal failed, 2 still
#   alive — on 1 and 2 the sentinel stays, so a retry can still name the watcher.
# watcher_core_alive
#   Is THIS install's core watcher confirmed alive? The identity this process's
#   environment states (the default core when unset), through the same sequence.
#   rc 0 alive (`pid N` on stdout), 1 not (why on stdout), 2 unknown — unknown
#   is never "not alive": a re-arm on it would start a duplicate consumer.
#   Executed (`watcher_identity.sh core-alive`), this is the desktop watchdog's
#   probe, so a peer worker's watcher cannot stand in for a missing core one.

_wi_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=watcher_sentinel.sh
. "$_wi_here/watcher_sentinel.sh"
# Only when no caller has loaded the seam yet: restart.sh loads it (or an
# injected fake) first, and loading it again would put the real one back.
if ! command -v pops_signal >/dev/null 2>&1; then
  # shellcheck source=process-ops.sh
  . "$_wi_here/process-ops.sh"
fi

watcher_confirm_owner() {
  local sentinel="$1" want_instance="${2:-}" want_workspace="${3:-}" code_path="${4:-}"
  local py owner pid rec_code rec_inc argv vector inc_file wrote_rc=0
  local -a vec_opt=()
  WATCHER_OWNER_PID=""; WATCHER_OWNER_INCARNATION=""; WATCHER_OWNER_REASON=""
  # shellcheck source=../scripts/python-binary.sh
  . "$_wi_here/../scripts/python-binary.sh" 2>/dev/null || true
  py="$(resolve_python "$_wi_here/.." 2>/dev/null || true)"
  if [ -z "$py" ]; then
    WATCHER_OWNER_REASON="no runnable python3 — the ownership policy cannot be asked, so nothing is ours to signal"
    return 1
  fi
  inc_file="$(sentinel_incarnation_path "$sentinel")"
  # The record half: a COMPLETE identity naming this install, this instance,
  # this checkout's script and the incarnation the live marker exposes.
  if ! owner="$("$py" "$_wi_here/watcher_identity.py" owner-pid \
                  --sentinel "$sentinel" --instance "$want_instance" \
                  --workspace "$want_workspace" --incarnation-file "$inc_file" \
                  --code-path "$code_path" 2>&1)"; then
    WATCHER_OWNER_REASON="$owner"
    return 1
  fi
  IFS=$'\t' read -r pid rec_code rec_inc <<< "$owner"
  if ! pops_alive "$pid"; then
    WATCHER_OWNER_REASON="pid $pid is not alive"
    return 1
  fi
  # The process half: the EXECUTED script, never containment. `python3 -c pass
  # /x/watch-tasks-stream.sh` carries that path as data and must not confirm.
  argv="$(pops_argv "$pid")"
  # The kernel's argv LIST when readable: the flattened string cannot split
  # `bash <script> <tasks-dir>`, the notifier's and the Monitor's real launch.
  if vector="$(pops_argv_vector "$pid")" && [ -n "$vector" ]; then
    vec_opt=(--argv-vector "$vector")
  fi
  # Neither read answered: a denied `ps` is not "not a watcher".
  if [ -z "$argv" ] && [ -z "$vector" ]; then
    WATCHER_OWNER_REASON="argv: pid $pid's argv is UNMEASURABLE (ps answered nothing and no argv vector could be read) — an unprovable owner is a refusal"
    return 1
  fi
  if ! WATCHER_OWNER_REASON="$("$py" "$_wi_here/watcher_identity.py" runs-watcher \
                  --pid "$pid" --argv "$argv" ${vec_opt[@]+"${vec_opt[@]}"} \
                  --code-path "$rec_code" 2>&1)"; then
    return 1
  fi
  # The age half; errexit-safe, since a bare call would abort the caller on
  # its rc 1 (reissued) or 2 (unknown).
  sentinel_pid_wrote_file "$pid" "$sentinel" || wrote_rc=$?
  if [ "$wrote_rc" -eq 1 ]; then
    WATCHER_OWNER_REASON="stale sentinel: pid $pid started AFTER $sentinel was stamped — a reissued pid, not its owner"
    return 1
  fi
  if [ "$wrote_rc" -ne 0 ]; then
    WATCHER_OWNER_REASON="stale sentinel: whether pid $pid wrote $sentinel is UNMEASURABLE — an unprovable owner is a refusal"
    return 1
  fi
  WATCHER_OWNER_REASON=""
  WATCHER_OWNER_PID="$pid"
  WATCHER_OWNER_INCARNATION="$rec_inc"
  return 0
}

watcher_stop_owned() {
  local sentinel="$1" pid="$2" inc="${3-${WATCHER_OWNER_INCARNATION:-}}"
  local tries="${SUTANDO_WATCHER_STOP_TICKS:-30}" i=0
  pops_signal "$pid" TERM || return 1
  while [ "$i" -lt "$tries" ]; do
    pops_alive "$pid" || break
    pops_grace_tick
    i=$((i + 1))
  done
  if pops_alive "$pid"; then
    return 2
  fi
  # rc 1 = a successor's record holds the path: the stop succeeded, the
  # record is not ours to remove.
  sentinel_release_incarnation "$sentinel" "$pid" "$inc" || true
  return 0
}

watcher_core_alive() {
  local ws sentinel
  # The same resolution restart.sh uses, so the app and a stop name one file.
  ws="$(bash "$_wi_here/../scripts/sutando-config.sh" workspace 2>/dev/null)"
  if [ -z "$ws" ]; then
    echo "workspace unresolved — the core's sentinel cannot be named"
    return 2
  fi
  if ! sentinel="$(sentinel_path_for "$ws/state" 2>/dev/null)" || [ -z "$sentinel" ]; then
    echo "could not resolve the core watcher sentinel under $ws/state"
    return 2
  fi
  if [ ! -f "$sentinel" ]; then
    echo "no core watcher record at $sentinel"
    return 1
  fi
  if watcher_confirm_owner "$sentinel" "$(sentinel_instance_from_path "$sentinel")" \
       "$ws" "$_wi_here/watch-tasks-stream.sh"; then
    echo "pid $WATCHER_OWNER_PID"
    return 0
  fi
  echo "$WATCHER_OWNER_REASON"
  case "$WATCHER_OWNER_REASON" in
    *UNMEASURABLE*|*"no runnable python3"*) return 2 ;;
  esac
  return 1
}

# Executed, not sourced: the desktop watchdog's probe.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    core-alive) watcher_core_alive; exit $? ;;
    *) echo "usage: watcher_identity.sh core-alive" >&2; exit 64 ;;
  esac
fi

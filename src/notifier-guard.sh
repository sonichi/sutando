# shellcheck shell=bash
# Shared by the task notifiers and their supervisor: when an events stream has ended, and a bounded stop.

# A process that exited but was not reaped still answers kill -0.
notifier_proc_is_zombie() {
  case "$(LC_ALL=C ps -o stat= -p "$1" 2>/dev/null | sed 's/^ *//')" in Z*) return 0 ;; esac
  return 1
}

notifier_proc_live() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null && ! notifier_proc_is_zombie "$1"
}

# After a timed read of the events FIFO returned non-zero ($1 writer pid, $2 seconds taken, $3 timeout):
# ended when the writer is gone, or the read returned early, which is EOF since no writer reopens it.
events_stream_ended() {
  notifier_proc_live "$1" || return 0
  [ "$3" -ge 2 ] && [ "$2" -lt $(( $3 - 1 )) ] && return 0
  return 1
}

# Whether any member of process group $1 is still running (kill -0 to the group).
notifier_group_live() {
  kill -0 "-$1" 2>/dev/null
}

# TERM $1's process group (else $1 alone), wait up to $2 s (default 3) for all of it, then KILL what is left.
# A group outlives its leader: members are waited on and killed whether or not the leader is gone.
stop_group_bounded() {
  local pid="${1:-}" grace="${2:-3}" deadline group=0
  [ -n "$pid" ] || return 0
  notifier_group_live "$pid" && group=1
  if [ "$group" = 1 ]; then kill -TERM "-$pid" 2>/dev/null || true; else kill -TERM "$pid" 2>/dev/null || true; fi
  deadline=$((SECONDS + grace))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if [ "$group" = 1 ]; then notifier_group_live "$pid" || break; else notifier_proc_live "$pid" || break; fi
    sleep 0.1
  done
  if [ "$group" = 1 ] && notifier_group_live "$pid"; then kill -KILL "-$pid" 2>/dev/null || true; fi
  if notifier_proc_live "$pid"; then kill -KILL "$pid" 2>/dev/null || true; fi
  wait "$pid" 2>/dev/null || true
}

# A signal trap's exit while `read -t` waits leaves bash 3.2's read timeout armed; it then spins in cleanup.
# So during the events read a signal is only recorded; the caller exits once the read returns.
NOTIFIER_IN_READ=0
NOTIFIER_STOP=""
notifier_signal() {
  if [ "$NOTIFIER_IN_READ" = 1 ]; then NOTIFIER_STOP="$1"; else exit "$1"; fi
}

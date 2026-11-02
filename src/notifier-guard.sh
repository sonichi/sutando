# Sourced by the task notifiers (claude, codex) and their supervisor. Bash 3.2 safe.
#
# 2026-11-01: a claude notifier spun at 60-90% CPU for about 9 h and ignored TERM. Its standby
# watcher had closed the events FIFO but not finished exiting. Every timed `read` then
# returned EOF at once. bash 3.2 reports EOF as 1, the same as a timeout, and `kill -0`
# on the dying watcher still succeeded, so the loop went round with no sleep. TERM then
# ran a cleanup that `wait`ed on that same watcher forever, so the notifier never exited,
# and neither did the supervisor waiting on the notifier. These helpers end both: a
# stream that has ended is recognised as ended, and every stop is bounded.

# A process that exited but was not reaped still answers kill -0.
notifier_proc_is_zombie() {
  case "$(LC_ALL=C ps -o stat= -p "$1" 2>/dev/null | sed 's/^ *//')" in Z*) return 0 ;; esac
  return 1
}

notifier_proc_live() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null && ! notifier_proc_is_zombie "$1"
}

# After a timed `read` from the events FIFO returned non-zero: has the stream ended?
# $1 writer pid, $2 seconds the read took, $3 its timeout. The stream has ended when its
# writer is gone or a zombie, or when the read came back well before its timeout. That is
# EOF, not a timeout: no writer ever reopens this FIFO, so every later read is EOF too.
# A timeout under 2 s cannot be told from EOF by time; the caller's 1 s backoff keeps
# that case from spinning, and the writer check still ends it.
events_stream_ended() {
  notifier_proc_live "$1" || return 0
  [ "$3" -ge 2 ] && [ "$2" -lt $(( $3 - 1 )) ] && return 0
  return 1
}

# Stop a child that leads its own process group: TERM the group (else the pid), wait up
# to $2 seconds (default 3) of wall time, then KILL. A child that ignores TERM can no
# longer hold its parent in `wait` forever. The wait polls with kill -0, a builtin, and
# asks `ps` about a zombie only about once a second: a fork per poll under load would
# stretch the grace far past its seconds.
stop_group_bounded() {
  local pid="${1:-}" grace="${2:-3}" deadline n=0
  [ -n "$pid" ] || return 0
  kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  deadline=$((SECONDS + grace))
  while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
    n=$((n + 1))
    if [ $((n % 10)) -eq 0 ] && notifier_proc_is_zombie "$pid"; then break; fi
    sleep 0.1
  done
  if notifier_proc_live "$pid"; then
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
  wait "$pid" 2>/dev/null || true
}

#!/bin/bash
# One bounded wait for the watcher's inline child runs (resolver, task handler).
# Sourcing this file defines wait_bounded and nothing else.
#
# wait_bounded <pid> <timeout_s> [<timeout_flag>]: waits for the child; past the
# bound it is TERMed, then KILLed. Returns the child's status. With a flag path
# given, that file exists afterwards only if the bound fired. Never plain
# `timeout`: not reliably present, and with no kill-after it leaves a
# TERM-resistant child unbounded anyway.
wait_bounded() {
  local pid="$1" limit="$2" timeout_flag="${3:-}" done_flag ticks watchdog_pid rc
  done_flag="$(mktemp -u "${TMPDIR:-/tmp}/sutando-bounded-done.XXXXXX")"
  ticks="$(awk -v l="$limit" 'BEGIN { printf "%d", l * 20 }')"
  # The watchdog is stood down through the flag, never signalled: a TERM that
  # reaches a subshell before bash has reset its inherited traps is dropped, or
  # runs the PARENT's trap in the child (the watcher's cleanup killed the watcher).
  ( trap 'kill "${_s:-}" 2>/dev/null; exit 0' TERM
    while [ "$ticks" -gt 0 ] && [ ! -e "$done_flag" ]; do
      sleep 0.05 & _s=$!; wait "$_s"
      ticks=$(( ticks - 1 ))
    done
    [ -e "$done_flag" ] && exit 0
    # Flag BEFORE killing, so the caller can tell "we gave up waiting" apart
    # from the child's own exit or signal.
    [ -z "$timeout_flag" ] || : > "$timeout_flag"
    kill -TERM "$pid" 2>/dev/null; sleep 1
    kill -KILL "$pid" 2>/dev/null ) &
  watchdog_pid=$!
  wait "$pid" 2>/dev/null
  rc=$?
  : > "$done_flag"
  wait "$watchdog_pid" 2>/dev/null
  rm -f "$done_flag"
  return "$rc"
}

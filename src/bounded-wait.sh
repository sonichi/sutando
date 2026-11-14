#!/bin/bash
# Shared bounded child run. run_bounded <timeout_s> [<timeout_flag>] -- cmd...: cmd runs as a direct
# child bounded to N whole seconds, then TERM, then KILL, by jobspec never by pid; its status; flag only if the bound fired.
run_bounded() {
  local limit="$1" timeout_flag="" pid
  shift
  if [ "$1" != "--" ]; then timeout_flag="$1"; shift; fi
  [ "$1" = "--" ] && shift
  # Whole decimal seconds (08 is 8), fraction truncated, floor 1. Non-digits never
  # reach $(( )): its error abandons the enclosing -c string or function, rc 1.
  limit="${limit%%.*}"
  case "${limit:-1}" in
    ''|*[!0-9]*) limit=1 ;;
    *) limit="$(( 10#${limit:-1} ))" ;;
  esac
  [ "$limit" -ge 1 ] 2>/dev/null || limit=1
  # The token is the job's identity for jobs/kill below; a caller's other
  # background job must not carry it in its command line.
  ( : sutando_run_bounded; exec "$@" ) &
  pid=$!
  if ! _rb_ended "$limit"; then
    # Flag BEFORE killing, so the caller can tell "we gave up waiting" apart
    # from the child's own exit or signal.
    [ -z "$timeout_flag" ] || : > "$timeout_flag"
    kill -TERM '%?sutando_run_bounded' 2>/dev/null
    _rb_ended 1 || kill -KILL '%?sutando_run_bounded' 2>/dev/null
  fi
  wait "$pid" 2>/dev/null
}

# _rb_ended <seconds>: 0 once bash's own job table no longer lists the job as running or
# stopped, 1 if it still does past the bound: never before it, late by at most min(poll overhead, 1s).
_rb_ended() {
  local limit="$1" start="$SECONDS" spent=0 polls=0 step=1 pause
  while :; do
    # The listing's subshell would print the job's own death notice on the way out.
    case "$(exec 2>/dev/null; jobs -r; jobs -s)" in *sutando_run_bounded*) ;; *) return 0 ;; esac
    [ "$spent" -lt $(( limit * 100 )) ] && [ $(( SECONDS - start )) -le "$limit" ] || return 1
    printf -v pause '0.%02d' "$step"
    sleep "$pause"
    spent=$(( spent + step ))
    # 10 ms polls for the first ~100 ms (a handler takes ~40), then 20, 40, 50.
    polls=$(( polls + 1 ))
    [ "$polls" -ge 4 ] && [ "$step" -lt 5 ] && { step=$(( step * 2 )); [ "$step" -gt 5 ] && step=5; }
  done
}

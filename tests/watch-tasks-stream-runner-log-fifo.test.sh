#!/usr/bin/env bash
# A FIFO at the runner's diagnostic log path must not stop the handler.
#
# The runner opened that path twice -- once on fd 3 before invoking the handler,
# once to append the RUNNER line afterwards. Opening a FIFO for write BLOCKS
# until a reader arrives, so a pipe left at that mutable workspace path held the
# runner and, with TASK_HANDLER_WORKERS=2, both worker slots: later
# handler-eligible tasks never ran and no fallback applied.
#
# Bounded on purpose: the failure mode IS a hang, so every case runs under a
# hard time limit and a case that exceeds it is a failure, not a slow pass.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

note() { printf '  %s %s\n' "$1" "$2"; }
fail() { note "FAIL" "$1"; FAILURES=$((FAILURES + 1)); }
ok()   { note "ok  " "$1"; }

# `timeout` is absent on macOS, so bound it with a watchdog killing the runner.
run_bounded() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local dog=$!
  wait "$pid" 2>/dev/null
  local rc=$?
  kill "$dog" 2>/dev/null
  return $rc
}

one_case() {
  # $1 = what sits at the log path: "fifo" | "regular" | "absent"
  local kind="$1"
  local ws; ws="$(mktemp -d)"
  mkdir -p "$ws/logs" "$ws/tasks" "$ws/results"
  local marker="$ws/handler-ran"
  local handler="$ws/handler.sh"
  printf '#!/bin/sh\ntouch %s\necho diag >&2\nexit 0\n' "$marker" > "$handler"
  chmod +x "$handler"
  printf 'id: task-1\ntask: body\n' > "$ws/tasks/task-1.txt"

  local log="$ws/logs/task-event-handler-runner.log"
  case "$kind" in
    fifo)    mkfifo "$log" ;;
    regular) : > "$log" ;;
    absent)  : ;;
  esac

  local fifo="$ws/events"
  mkfifo "$fifo"
  # Drain the events pipe so HANDLER_DONE never blocks on its own reader.
  ( cat "$fifo" > "$ws/events.out" ) &
  local drain=$!

  run_bounded 10 bash "$REPO/src/watch-tasks-stream.sh" --handler-runner \
    "$handler" "" "$ws" "$ws/tasks/task-1.txt" "$ws/results" "$REPO" "$fifo" "task-1.txt"
  local rc=$?
  kill "$drain" 2>/dev/null; wait "$drain" 2>/dev/null

  local ran="no" done_line="none"
  [ -e "$marker" ] && ran="yes"
  grep -q 'HANDLER_DONE: 0' "$ws/events.out" 2>/dev/null && done_line="HANDLER_DONE: 0"
  printf '%s|%s|%s\n' "$rc" "$ran" "$done_line"
  rm -rf "$ws"
}

# The regression: a FIFO with NO reader must still let the handler run and the
# completion reach the events pipe.
got="$(one_case fifo)"
case "$got" in
  0\|yes\|HANDLER_DONE:\ 0) ok "a FIFO at the log path does not stop the handler" ;;
  *) fail "FIFO case: got '$got', want '0|yes|HANDLER_DONE: 0'" ;;
esac

# Controls: the ordinary sinks must be unchanged by the guard, or the case above
# would pass for a runner that stopped logging entirely.
got="$(one_case regular)"
case "$got" in
  0\|yes\|HANDLER_DONE:\ 0) ok "control: a regular log still completes" ;;
  *) fail "regular case: got '$got'" ;;
esac

got="$(one_case absent)"
case "$got" in
  0\|yes\|HANDLER_DONE:\ 0) ok "control: an absent log still completes" ;;
  *) fail "absent case: got '$got'" ;;
esac

if [ "$FAILURES" -eq 0 ]; then
  echo "PASS — the diagnostic sink cannot hold the handler runner"
  exit 0
fi
echo "FAILED — $FAILURES case(s)"
exit 1

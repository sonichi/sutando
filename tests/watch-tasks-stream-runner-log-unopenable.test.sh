#!/usr/bin/env bash
# The runner's diagnostic log is best-effort: a log target that cannot be opened
# must not turn a successful handler into HANDLER_DONE: 1.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
WS="$TMP/ws"; mkdir -p "$WS/tasks" "$WS/results" "$WS/logs/task-event-handler-runner.log"   # the log path is a DIRECTORY
printf 'id: task-demo\ntask: x\n' > "$WS/tasks/task-demo.txt"
H="$TMP/handler.sh"; printf '#!/bin/sh\necho "handler stderr line" >&2\nexit 0\n' > "$H"; chmod +x "$H"
FIFO="$TMP/events"; mkfifo "$FIFO"
( cat "$FIFO" > "$TMP/events.out" & ) ; sleep 0.2
bash "$REPO/src/watch-tasks-stream.sh" --handler-runner "$H" claude "$WS" "$WS/tasks/task-demo.txt" "$WS/results" "$REPO" "$FIFO" task-demo.txt 2>"$TMP/runner.stderr"
sleep 0.3
grep -q '^HANDLER_DONE: 0 task-demo.txt$' "$TMP/events.out"; check $? "unopenable log: a successful handler still reports HANDLER_DONE: 0 ($(cat "$TMP/events.out" 2>/dev/null | tr '\n' ' '))"
grep -q 'handler stderr line' "$TMP/runner.stderr"; check $? "unopenable log: the handler's stderr falls back to the inherited stderr"
# control: an openable log captures stderr and the RUNNER line
rm -rf "$WS/logs/task-event-handler-runner.log"; FIFO2="$TMP/events2"; mkfifo "$FIFO2"; ( cat "$FIFO2" > "$TMP/events2.out" & ); sleep 0.2
bash "$REPO/src/watch-tasks-stream.sh" --handler-runner "$H" claude "$WS" "$WS/tasks/task-demo.txt" "$WS/results" "$REPO" "$FIFO2" task-demo.txt 2>/dev/null
sleep 0.3
grep -q '^HANDLER_DONE: 0 task-demo.txt$' "$TMP/events2.out"; check $? "openable log: HANDLER_DONE: 0"
grep -q 'handler stderr line' "$WS/logs/task-event-handler-runner.log" && grep -q 'RUNNER rc=0 task-demo.txt' "$WS/logs/task-event-handler-runner.log"; check $? "openable log: stderr and the RUNNER rc line are captured"
echo "$pass passed, $fail failed"; [ "$fail" = 0 ]

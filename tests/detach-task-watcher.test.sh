#!/usr/bin/env bash
# A detached watcher outlives the session that started it, appends its events to
# one log per inbox, and a second call over a ready one starts nothing.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
DETACH="$REPO/src/detach-task-watcher.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-detach.XXXXXX")"
mkdir -p "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL -- "-$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$1" = 0 ]; then echo "  PASS $2"; else echo "  FAIL $2${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
detach() {  # detach <ws> <inbox> <outfile>
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$1" PATH="$WORK/stubbin:$PATH" \
      bash "$DETACH" --inbox "$2" --workspace "$1" > "$3" 2> "${3%.out}.err"
}

echo "detach-task-watcher:"
WS="$WORK/ws"; mkdir -p "$WS/tasks" "$WS/state" "$WS/logs"

# (a) It starts one, reports the log and the pid, and the watcher is READY.
detach "$WS" "$WS/tasks" "$WORK/a.out"; rc=$?
check "$rc" "(a) the first call exits 0" "$(tail -2 "$WORK/a.err" | tr '\n' '|')"
LOG="$(sed -n 's/^LOG: //p' "$WORK/a.out")"
PID="$(sed -n 's/^PID: //p' "$WORK/a.out")"
PIDS+=("$PID")
[ -n "$LOG" ] && [ -n "$PID" ] && alive "$PID"; check $? "(a) it reports a log and a live pid ($PID)" "out: $(tr '\n' '|' < "$WORK/a.out")"
[ "$(python3 "$REPO/src/watcher_identity.py" role-present session --inbox "$WS/tasks" --ready "$WS/state")" = "yes" ]
check $? "(a) ...and the watcher is ready, not merely spawned"
[ "$LOG" = "$(python3 "$REPO/src/util_paths.py" watcher-log "$WS" "$WS/tasks")" ]
check $? "(a) ...at the path util_paths names for this inbox"

# (b) It is DETACHED: not in the caller's process group, and its parent is gone.
[ "$(ps -o pgid= -p "$PID" | tr -d ' ')" != "$(ps -o pgid= -p $$ | tr -d ' ')" ]
check $? "(b) the watcher is in its own process group, not the caller's"
[ "$(ps -o ppid= -p "$PID" | tr -d ' ')" = 1 ]
check $? "(b) ...and is reparented, so the starting shell's exit cannot take it" "ppid=$(ps -o ppid= -p "$PID" | tr -d ' ')"

# (c) Events land in the log, which is what a re-armed tail replays from.
: > "$WS/tasks/task-one.txt"
for i in $(seq 1 100); do grep -q 'task-one.txt' "$LOG" 2>/dev/null && break; sleep 0.1; done
grep -q 'TASK_FILE: .*task-one.txt' "$LOG"; check $? "(c) a delivery is appended to the log" "log: $(tr '\n' '|' < "$LOG")"
BEFORE="$(wc -l < "$LOG" | tr -d ' ')"
: > "$WS/tasks/task-two.txt"
for i in $(seq 1 100); do [ "$(wc -l < "$LOG" | tr -d ' ')" -gt "$BEFORE" ] && break; sleep 0.1; done
[ "$(wc -l < "$LOG" | tr -d ' ')" -gt "$BEFORE" ]; check $? "(c) ...and the log grows, so a tail from line $BEFORE replays the gap"

# (d) A second call over a ready watcher starts nothing and says so.
detach "$WS" "$WS/tasks" "$WORK/d.out"; rc=$?
check "$rc" "(d) a second call exits 0" "$(tail -2 "$WORK/d.err" | tr '\n' '|')"
[ "$(sed -n 's/^PID: //p' "$WORK/d.out")" = "$PID" ]; check $? "(d) ...reporting the SAME pid, not a second watcher" "got $(sed -n 's/^PID: //p' "$WORK/d.out")"
grep -q 'already has a ready session watcher' "$WORK/d.err"; check $? "(d) ...and says nothing was started"
# By holder, not by pgrep: pgrep -f also matches the python exec wrapper's argv.
[ "$(python3 "$REPO/src/watcher_identity.py" inbox-holders --inbox "$WS/tasks" | grep -c 'session$')" = 1 ]
check $? "(d) ...leaving exactly one session watcher on the inbox" "holders: $(python3 "$REPO/src/watcher_identity.py" inbox-holders --inbox "$WS/tasks" | tr '\n' '|')"

# (e) Two inboxes get two logs: one session tailing its own never sees the other's.
mkdir -p "$WS/deliveries/w1"
detach "$WS" "$WS/deliveries/w1" "$WORK/e.out"
LOG2="$(sed -n 's/^LOG: //p' "$WORK/e.out")"; P2="$(sed -n 's/^PID: //p' "$WORK/e.out")"; PIDS+=("$P2")
[ -n "$LOG2" ] && [ "$LOG2" != "$LOG" ]; check $? "(e) a second inbox gets its own log" "$LOG2"
: > "$WS/deliveries/w1/task-w.txt"
for i in $(seq 1 100); do grep -q 'task-w.txt' "$LOG2" 2>/dev/null && break; sleep 0.1; done
grep -q 'task-w.txt' "$LOG2" && ! grep -q 'task-w.txt' "$LOG"; check $? "(e) ...and its events stay out of the first"

# (f) Usage: no inbox anywhere is a refusal, not a guess.
env -u SUTANDO_TASKS_DIR bash "$DETACH" > "$WORK/f.out" 2> "$WORK/f.err"; rc=$?
[ "$rc" = 64 ] && grep -q 'no --inbox' "$WORK/f.err"; check $? "(f) no inbox and no SUTANDO_TASKS_DIR exits 64" "rc=$rc $(tail -1 "$WORK/f.err")"

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

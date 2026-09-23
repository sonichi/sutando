#!/usr/bin/env bash
# The reader of a detached watcher's log keeps its own cursor, so a re-arm
# replays exactly the lines it never consumed; and that cursor's freshness is
# what tells the supervisor whether anyone is reading at all.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
TAILER="$REPO/src/tail-watcher-log.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-tailcur.XXXXXX")"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.4
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$1" = 0 ]; then echo "  PASS $2"; else echo "  FAIL $2${3:+ — $3}"; fail=1; fi; }
WS="$WORK/ws"; mkdir -p "$WS/tasks" "$WS/state" "$WS/logs"
LOG="$(python3 "$REPO/src/util_paths.py" watcher-log "$WS" "$WS/tasks")"
CUR="$(python3 "$REPO/src/util_paths.py" watcher-log-cursor "$WS" "$WS/tasks")"
run() {  # run <outfile>; prints the pid
  bash "$TAILER" --inbox "$WS/tasks" --workspace "$WS" > "$1" 2>> "$WORK/err" &
  echo $!
}
settle() { local i; for i in $(seq 1 60); do [ "$(cat "$CUR" 2>/dev/null)" = "$1" ] && return 0; sleep 0.1; done; return 1; }

echo "tail-watcher-log:"
printf 'TASK_FILE: one\nTASK_FILE: two\n' > "$LOG"
P="$(run "$WORK/a.out")"; PIDS+=("$P")
settle 2; check $? "(a) the first reader consumes the whole log (cursor $(cat "$CUR" 2>/dev/null))" "$(tail -2 "$WORK/err" 2>/dev/null)"
[ "$(tr '\n' '|' < "$WORK/a.out")" = "TASK_FILE: one|TASK_FILE: two|" ]
check $? "(a) ...and emits both lines" "got [$(tr '\n' '|' < "$WORK/a.out")]"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null

# The gap: lines that arrive while nothing is reading are the ones a length-based
# cursor would skip. They are exactly what nothing else announces.
printf 'TASK_FILE: three\nTASK_FILE: four\n' >> "$LOG"
P="$(run "$WORK/b.out")"; PIDS+=("$P")
settle 4; check $? "(b) the re-armed reader picks up at line 3" "cursor=$(cat "$CUR" 2>/dev/null)"
[ "$(tr '\n' '|' < "$WORK/b.out")" = "TASK_FILE: three|TASK_FILE: four|" ]
check $? "(b) ...replaying the gap and nothing else" "got [$(tr '\n' '|' < "$WORK/b.out")]"

# Liveness: the cursor is touched while idle, so "someone is reading" is readable.
BEFORE="$(stat -c %Y "$CUR" 2>/dev/null || stat -f %m "$CUR")"
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/tasks" --ready "$WS/state")" = "yes" ]
check $? "(c) a live reader reads as fresh"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null
sleep 0.3
[ -z "$(pgrep -f "tail -n \+.*$(basename "$LOG")" 2>/dev/null)" ]
check $? "(c) ...and its tail dies with it, leaving none behind" "left: $(pgrep -f "tail -n \+.*$(basename "$LOG")" | tr '\n' ' ')"
python3 - "$CUR" <<'EOF'
import os, sys, time
os.utime(sys.argv[1], (time.time() - 600, time.time() - 600))
EOF
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/tasks" --ready "$WS/state")" = "no" ]
check $? "(c) a reader that stopped touching it reads as stale, which is what the supervisor gates on"

# An inbox nobody ever read detached has no cursor: that is "unknown", not "no",
# because a Monitor-hosted watcher needs no cursor to be covering its inbox.
mkdir -p "$WS/deliveries/w9"
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/deliveries/w9" --ready "$WS/state")" = "unknown" ]
check $? "(d) an inbox with no cursor answers unknown, never no"

# A cursor past the end of the log (rotated or replaced) restarts rather than skips.
: > "$LOG"; printf 'TASK_FILE: fresh-start\n' >> "$LOG"
P="$(run "$WORK/e.out")"; PIDS+=("$P")
settle 1; check $? "(e) a cursor past the end restarts at the top" "cursor=$(cat "$CUR" 2>/dev/null)"
grep -q 'fresh-start' "$WORK/e.out"; check $? "(e) ...so a replaced log is not skipped" "got [$(tr '\n' '|' < "$WORK/e.out")]"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

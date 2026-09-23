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
# The cursor is "<lines> <inode>"; the tests care about the count.
cur_n() { awk '{print $1}' "$CUR" 2>/dev/null; }
settle() { local i; for i in $(seq 1 60); do [ "$(cur_n)" = "$1" ] && return 0; sleep 0.1; done; return 1; }
# Wait for the OUTPUT, never for a cursor value the previous reader already left:
# `settle 1` after a rotation returns before the new reader has even started.
emitted() { local i; for i in $(seq 1 60); do grep -q "$2" "$1" 2>/dev/null && return 0; sleep 0.1; done; return 1; }

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
settle 4; check $? "(b) the re-armed reader picks up at line 3" "cursor=$(cur_n)"
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

# (c2) A SIGKILLed reader runs no trap: its heartbeat must not keep the cursor
#      fresh beside a reader that is gone. This is the failure that hid a dead
#      reader from both the Stop hook and the supervisor.
: > "$LOG"; printf 'TASK_FILE: k1\n' >> "$LOG"; printf '%s %s' 0 "$(ls -di "$LOG" | awk '{print $1}')" > "$CUR"
P="$(run "$WORK/k.out")"; PIDS+=("$P")
settle 1; check $? "(c2) a reader is up on the log" "cursor=$(cur_n)"
kill -KILL "$P" 2>/dev/null; wait "$P" 2>/dev/null
M0="$(stat -c %Y "$CUR" 2>/dev/null || stat -f %m "$CUR")"
sleep "$(( ${SUTANDO_TAIL_HEARTBEAT_SEC:-2} + 2 ))"
M1="$(stat -c %Y "$CUR" 2>/dev/null || stat -f %m "$CUR")"
[ "$M0" = "$M1" ]; check $? "(c2) the cursor stops being touched once the reader is SIGKILLed" "mtime $M0 -> $M1"
[ -z "$(pgrep -f "tail -n \+.*$(basename "$LOG")" 2>/dev/null)" ]
check $? "(c2) ...and the orphaned tail is taken with it" "left: $(pgrep -f "tail -n \+.*$(basename "$LOG")" | tr '\n' ' ')"

# An inbox nobody ever read detached has no cursor: that is "unknown", not "no",
# because a Monitor-hosted watcher needs no cursor to be covering its inbox.
mkdir -p "$WS/deliveries/w9"
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/deliveries/w9" --ready "$WS/state")" = "unknown" ]
check $? "(d) an inbox with no cursor answers unknown, never no"

# (d2) A cursor left by an earlier detached period must not make a LEGACY
#      Monitor-hosted watcher read as uncovered: its coverage needs no reader.
python3 - "$CUR" <<'PYEOF2'
import os, sys, time
os.utime(sys.argv[1], (time.time() - 600, time.time() - 600))
PYEOF2
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/tasks" --ready "$WS/state")" = "no" ]
check $? "(d2) without a holder, a stale cursor still answers no"
SELF=$$
[ "$(python3 "$REPO/src/watcher_identity.py" reader-fresh --inbox "$WS/tasks" --ready "$WS/state" --holder "$SELF")" = "unknown" ]
check $? "(d2) ...but a holder whose output is not that log answers unknown, so a live legacy watcher keeps its coverage"

# (e) Rotation, the two shapes a reader can actually tell apart. A line count
#     alone reads both as "nothing new".
#     Its own workspace: the SIGKILL case above leaves a tail being reaped, and
#     a rotation test must measure rotation, not that cleanup's timing.
WS="$WORK/ws-rot"; mkdir -p "$WS/tasks" "$WS/state" "$WS/logs"
LOG="$(python3 "$REPO/src/util_paths.py" watcher-log "$WS" "$WS/tasks")"
CUR="$(python3 "$REPO/src/util_paths.py" watcher-log-cursor "$WS" "$WS/tasks")"
printf 'TASK_FILE: before\n' > "$LOG"
P="$(run "$WORK/e0.out")"; PIDS+=("$P")
settle 1; check $? "(e) setup: a reader consumed the pre-rotation log" "cursor=$(cur_n)"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null
#     1. REPLACED: a new file at the same path (new inode), whatever its length.
rm -f "$LOG"; printf 'TASK_FILE: after-replace\n' > "$LOG"
P="$(run "$WORK/e2.out")"; PIDS+=("$P")
emitted "$WORK/e2.out" 'after-replace'
check $? "(e) a REPLACED log is read from the top, so nothing in it is skipped" "cursor=$(cur_n) out=[$(tr '\n' '|' < "$WORK/e2.out")]"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null
#     2. TRUNCATED in place and seen while short: the reader starts over, and the
#        lines appended afterwards are read, not skipped.
: > "$LOG"
P="$(run "$WORK/e3.out")"; PIDS+=("$P")
settle 0; check $? "(e) a TRUNCATED log resets the cursor to 0" "cursor=$(cur_n)"
printf 'TASK_FILE: after-truncate\n' >> "$LOG"
emitted "$WORK/e3.out" 'after-truncate'
check $? "(e) ...and what arrives after it is read, not skipped" "cursor=$(cur_n) out=[$(tr '\n' '|' < "$WORK/e3.out")]"
kill -TERM "$P" 2>/dev/null; wait "$P" 2>/dev/null

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

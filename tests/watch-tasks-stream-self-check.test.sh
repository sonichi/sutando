#!/usr/bin/env bash
# The watcher enforces one announcer per inbox at its own startup:
#   (a) a second session watcher on a watched inbox exits 0 and leaves the first alone;
#   (b) --force-restart replaces exactly the holder: the first dies, the second runs;
#   (c) a session watcher over a standby holder proceeds: the handoff;
#   (d) a standby over a live session watcher exits 0;
#   (e) an untagged start is refused (rc 64): a watcher is started by its monitor and says so;
#   (f) a watcher on ANOTHER inbox is never a holder for this one;
#   (h) --force-restart aborts, signaling nothing, when a live holder can no longer be re-proven;
#   (i) a covered exit prints one WATCHER_HELD line on stdout naming the holder and the replace command;
#   (j) an untagged holder is nobody's standby: a session start over it exits 0 as covered;
#   (k) --force-restart replaces a standby holder too, not only a session one.
# Run: bash tests/watch-tasks-stream-self-check.test.sh
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-selfcheck.XXXXXX")"
mkdir -p "$WORK/a/tasks" "$WORK/a/state" "$WORK/b/tasks" "$WORK/b/state" "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"
chmod +x "$WORK/stubbin/fswatch"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
# Every watcher runs with this session's own identity stripped, so the inbox and
# the sentinel come from the arguments, never from a live worker's environment.
# Each watcher gets its own session: its cleanup runs `kill 0`, which would
# otherwise take this test's process group with it.
run_watcher() {  # run_watcher <ws> <errfile> <args...>; prints the pid
  local ws="$1" err="$2"; shift 2
  env -u SUTANDO_INSTANCE_ID -u AGENT_ID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$@" > /dev/null 2> "$err" &
  echo $!
}
run_watcher_fg() {  # same, in the foreground: returns the watcher's exit code
  local ws="$1" err="$2"; shift 2
  env -u SUTANDO_INSTANCE_ID -u AGENT_ID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$@" > /dev/null 2> "$err"
}
run_watcher_fg_out() {  # same as run_watcher_fg, stdout kept: run_watcher_fg_out <ws> <errfile> <outfile> <args...>
  # Bounded: a watcher expected to exit that is still up after 20 s is killed and
  # reads as rc 124, so a regression fails the case instead of hanging the suite.
  local ws="$1" err="$2" out="$3" p i=0; shift 3
  env -u SUTANDO_INSTANCE_ID -u AGENT_ID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$@" > "$out" 2> "$err" &
  p=$!
  while kill -0 "$p" 2>/dev/null && [ "$i" -lt 40 ]; do sleep 0.5; i=$((i + 1)); done
  if kill -0 "$p" 2>/dev/null; then
    kill -TERM -- "-$p" 2>/dev/null; kill -TERM "$p" 2>/dev/null; wait "$p" 2>/dev/null
    echo "run_watcher_fg_out: still running after 20 s; killed" >> "$err"
    return 124
  fi
  wait "$p"
}
alive() { kill -0 "$1" 2>/dev/null; }

echo "watch-tasks-stream self-check:"
# (a) first session watcher runs; the second exits 0 and names the holder.
A1=$(run_watcher "$WORK/a" "$WORK/a1.err" "$WORK/a/tasks" --role session --inbox "$WORK/a/tasks"); PIDS+=("$A1")
sleep 2
alive "$A1"; check "(a) the first session watcher is up" $?
run_watcher_fg "$WORK/a" "$WORK/a2.err" "$WORK/a/tasks" --role session --inbox "$WORK/a/tasks"; rc=$?
check "(a) the second session watcher exits 0" $([ "$rc" = 0 ] && echo 0 || echo 1) "rc=$rc"
grep -q "already watched by pid $A1 (session)" "$WORK/a2.err"; check "(a) ...and names the holder" $? "$(tail -1 "$WORK/a2.err")"
alive "$A1"; check "(a) the first is untouched" $?

# (b) --force-restart replaces the holder.
A3=$(run_watcher "$WORK/a" "$WORK/a3.err" "$WORK/a/tasks" --role session --inbox "$WORK/a/tasks" --force-restart); PIDS+=("$A3")
sleep 3
alive "$A1"; check "(b) --force-restart stopped the holder $A1" $([ $? = 0 ] && echo 1 || echo 0)
alive "$A3"; check "(b) ...and the replacement runs" $?
grep -q "stopping watcher pid $A1 (session)" "$WORK/a3.err"; check "(b) ...saying which pid it replaced" $?

# (e) an untagged start is refused before anything is touched: no role, no
# inbox tag, a tag that names another directory. Nothing runs, no sentinel.
run_watcher_fg "$WORK/b" "$WORK/e1.err" "$WORK/b/tasks"; rc=$?
check "(e) a start with no --role is refused with rc 64" $([ "$rc" = 64 ] && echo 0 || echo 1) "rc=$rc — $(tail -1 "$WORK/e1.err")"
run_watcher_fg "$WORK/b" "$WORK/e2.err" "$WORK/b/tasks" --role standby; rc=$?
check "(e) a start with no --inbox is refused with rc 64" $([ "$rc" = 64 ] && echo 0 || echo 1) "rc=$rc"
run_watcher_fg "$WORK/b" "$WORK/e3.err" "$WORK/b/tasks" --role standby --inbox "$WORK/a/tasks"; rc=$?
check "(e) a tag naming another directory is refused with rc 64" $([ "$rc" = 64 ] && echo 0 || echo 1) "rc=$rc"
grep -q "refusing to start" "$WORK/e1.err"; check "(e) ...and each says it is refusing" $?
[ ! -e "$WORK/b/state/watch-tasks-stream.pid" ]; check "(e) ...and no sentinel was written" $?

# (c) a session watcher over a standby holder proceeds (the supervisor stands it down).
B1=$(run_watcher "$WORK/b" "$WORK/b1.err" "$WORK/b/tasks" --role standby --inbox "$WORK/b/tasks"); PIDS+=("$B1")
sleep 2
alive "$B1"; check "(c) the standby holder is up" $?
B2=$(run_watcher "$WORK/b" "$WORK/b2.err" "$WORK/b/tasks" --role session --inbox "$WORK/b/tasks"); PIDS+=("$B2")
sleep 3
alive "$B2"; check "(c) a session watcher over a standby holder proceeds" $?
alive "$B1"; check "(c) ...and does not kill it (the supervisor's job)" $?

# (d) a standby over a live session watcher exits 0.
run_watcher_fg "$WORK/b" "$WORK/b3.err" "$WORK/b/tasks" --role standby --inbox "$WORK/b/tasks"; rc=$?
check "(d) a standby over a session watcher exits 0" $([ "$rc" = 0 ] && echo 0 || echo 1) "rc=$rc"
grep -q -E "already watched by pid ($B1 \(standby\)|$B2 \(session\))" "$WORK/b3.err"; check "(d) ...naming a holder of the inbox" $? "$(tail -1 "$WORK/b3.err")"

# (f) inbox a's watcher is not a holder for inbox b and vice versa.
alive "$A3" && alive "$B2"; check "(f) both inboxes keep their own watcher" $?

# (g) THE CI REGRESSION: an unreadable process table must not stop a start.
# Refusing there leaves the inbox with no announcer at all, which is the failure
# this whole mechanism exists to prevent.
mkdir -p "$WORK/c/tasks" "$WORK/c/state" "$WORK/nops"
printf '#!/bin/sh\nexit 1\n' > "$WORK/nops/ps"; chmod +x "$WORK/nops/ps"
C1=$(PATH="$WORK/nops:$PATH" run_watcher "$WORK/c" "$WORK/c1.err" "$WORK/c/tasks" --role session --inbox "$WORK/c/tasks"); PIDS+=("$C1")
sleep 3
alive "$C1"; check "(g) an unreadable process table still starts the watcher" $? "$(tail -1 "$WORK/c1.err")"
grep -q "could not read the process table" "$WORK/c1.err"; check "(g) ...and says the check was skipped" $?

# (h) THE REVIEW REGRESSION: the first scan finds a holder, then process
# inspection goes unobservable. --force-restart must abort (rc 3), leave the
# holder and its children alone, and start nothing; "unobserved" is not "gone".
mkdir -p "$WORK/d/tasks" "$WORK/d/state" "$WORK/blind"
cat > "$WORK/blind/ps" <<EOS
#!/bin/sh
case " \$* " in *" -Ao "*)
  n=\$(cat "$WORK/blind/n" 2>/dev/null || echo 0); n=\$((n+1)); echo "\$n" > "$WORK/blind/n"
  if [ "\$n" -le 1 ]; then for p in /bin/ps /usr/bin/ps; do [ -x "\$p" ] && exec "\$p" "\$@"; done; fi ;;
esac
exit 1
EOS
chmod +x "$WORK/blind/ps"
D1=$(run_watcher "$WORK/d" "$WORK/d1.err" "$WORK/d/tasks" --role session --inbox "$WORK/d/tasks"); PIDS+=("$D1")
sleep 2
alive "$D1"; check "(h) the holder is up" $?
D1KIDS="$(pgrep -P "$D1" -f stubbin/fswatch | tr '\n' ' ')"
# Time-bound: the defect this pins starts a replacement that never exits.
( PATH="$WORK/blind:$PATH" run_watcher_fg "$WORK/d" "$WORK/d2.err" "$WORK/d/tasks" --role session --inbox "$WORK/d/tasks" --force-restart; echo $? > "$WORK/d2.rc" ) &
for _ in $(seq 1 100); do [ -f "$WORK/d2.rc" ] && break; sleep 0.1; done
rc="$(cat "$WORK/d2.rc" 2>/dev/null || echo "still running")"
[ -f "$WORK/d2.rc" ] || pkill -TERM -f "force-restart" 2>/dev/null
check "(h) --force-restart with a blind revalidation exits 3" $([ "$rc" = 3 ] && echo 0 || echo 1) "rc=$rc — $(tail -1 "$WORK/d2.err")"
alive "$D1"; check "(h) ...the holder is untouched" $?
kids_ok=0; for k in $D1KIDS; do alive "$k" || kids_ok=1; done; check "(h) ...and so are its children" $kids_ok "kids=$D1KIDS"
grep -q "cannot be re-proven" "$WORK/d2.err"; check "(h) ...and it says why" $?

# (i) the covered exit tells the caller on stdout, in the TASK_FILE shape.
run_watcher_fg_out "$WORK/a" "$WORK/i.err" "$WORK/i.out" "$WORK/a/tasks" --role session --inbox "$WORK/a/tasks"; rc=$?
check "(i) a second session start still exits 0" "$rc"
grep -q "^WATCHER_HELD: inbox=$(cd "$WORK/a/tasks" && pwd -P) pid=$A3 role=session since=\"" "$WORK/i.out" && r=0 || r=1; check "(i) ...and prints WATCHER_HELD with the holder's pid and role" "$r" "$(cat "$WORK/i.out")"
grep -q ' read=\(yes\|no\|unknown\) replace="watch-tasks-stream.sh --force-restart --role session --inbox ' "$WORK/i.out" && r=0 || r=1; check "(i) ...with the read verdict and the replace command" "$r"

# (j) an untagged holder counts as covered: a session start over it exits 0.
# Untagged starts are refused since the tag became mandatory, so the holder is a
# real process shaped like one: a script named watch-tasks-stream.sh, inbox as
# its only operand, classified by the real identity code.
mkdir -p "$WORK/e/tasks" "$WORK/e/state" "$WORK/fake"; printf '#!/bin/bash\nsleep 60\n' > "$WORK/fake/watch-tasks-stream.sh"; chmod +x "$WORK/fake/watch-tasks-stream.sh"
bash "$WORK/fake/watch-tasks-stream.sh" "$WORK/e/tasks" & U1=$!; PIDS+=("$U1")
sleep 1
run_watcher_fg_out "$WORK/e" "$WORK/j.err" "$WORK/j.out" "$WORK/e/tasks" --role session --inbox "$WORK/e/tasks"; rc=$?
check "(j) a session start over an untagged holder exits 0" "$rc" "$(tail -1 "$WORK/j.err")"
grep -q "^WATCHER_HELD: .* pid=$U1 role=untagged " "$WORK/j.out" && r=0 || r=1; check "(j) ...naming the untagged holder" "$r" "$(cat "$WORK/j.out")"
alive "$U1"; check "(j) ...and leaves it running" $?

# (k) --force-restart replaces a standby holder, not only a session one.
mkdir -p "$WORK/f/tasks" "$WORK/f/state"
F1=$(run_watcher "$WORK/f" "$WORK/f1.err" "$WORK/f/tasks" --role standby --inbox "$WORK/f/tasks"); PIDS+=("$F1")
sleep 2
alive "$F1"; check "(k) the standby holder is up" $?
F2=$(run_watcher "$WORK/f" "$WORK/f2.err" "$WORK/f/tasks" --role session --inbox "$WORK/f/tasks" --force-restart); PIDS+=("$F2")
sleep 3
alive "$F1"; check "(k) --force-restart stopped the standby holder $F1" $([ $? = 0 ] && echo 1 || echo 0)
alive "$F2"; check "(k) ...and the replacement runs" $?
grep -q "stopping watcher pid $F1 (standby)" "$WORK/f2.err" && r=0 || r=1; check "(k) ...saying which pid it replaced" "$r"

if [ "$fail" = 0 ]; then echo "  ok  one announcer per inbox, enforced by the watcher"; else echo "  FAILED"; fi
exit "$fail"

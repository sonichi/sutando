#!/usr/bin/env bash
# The watcher enforces one announcer per inbox at its own startup:
#   (a) a second session watcher on a watched inbox exits 0 and leaves the first alone;
#   (b) --force-restart replaces exactly the holder: the first dies, the second runs;
#   (c) a session watcher over an untagged (standby-shaped) holder proceeds: the handoff;
#   (d) a standby over a live session watcher exits 0;
#   (e) an untagged start on a free inbox warns and runs;
#   (f) a watcher on ANOTHER inbox is never a holder for this one.
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

# (c) a session watcher over an untagged holder proceeds (the supervisor stands it down).
B1=$(run_watcher "$WORK/b" "$WORK/b1.err" "$WORK/b/tasks"); PIDS+=("$B1")
sleep 2
alive "$B1"; check "(e) an untagged start on a free inbox runs" $?
grep -q "untagged start" "$WORK/b1.err"; check "(e) ...with the tag warning" $?
B2=$(run_watcher "$WORK/b" "$WORK/b2.err" "$WORK/b/tasks" --role session --inbox "$WORK/b/tasks"); PIDS+=("$B2")
sleep 3
alive "$B2"; check "(c) a session watcher over an untagged holder proceeds" $?
alive "$B1"; check "(c) ...and does not kill it (the supervisor's job)" $?

# (d) a standby over a live session watcher exits 0.
run_watcher_fg "$WORK/b" "$WORK/b3.err" "$WORK/b/tasks" --role standby --inbox "$WORK/b/tasks"; rc=$?
check "(d) a standby over a session watcher exits 0" $([ "$rc" = 0 ] && echo 0 || echo 1) "rc=$rc"
grep -q -E "already watched by pid ($B1 \(untagged\)|$B2 \(session\))" "$WORK/b3.err"; check "(d) ...naming a holder of the inbox" $? "$(tail -1 "$WORK/b3.err")"

# (f) inbox a's watcher is not a holder for inbox b and vice versa.
alive "$A3" && alive "$B2"; check "(f) both inboxes keep their own watcher" $?

if [ "$fail" = 0 ]; then echo "  ok  one announcer per inbox, enforced by the watcher"; else echo "  FAILED"; fi
exit "$fail"

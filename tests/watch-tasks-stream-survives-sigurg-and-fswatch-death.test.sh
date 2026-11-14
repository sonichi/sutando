#!/usr/bin/env bash
# The session watcher outlives what used to end it mid-session. The real
# watch-tasks-stream.sh, on a scratch inbox, hosted the way the Monitor hosts
# it: a wrapper shell that LEADS the process group, the watcher a member of it.
#   (a) SIGURG to the watcher and to its fswatch changes nothing: both stay, the
#       next task is announced;
#   (b) its fswatch dying is not its end: fswatch is relaunched (stderr says so,
#       a new child appears) and the next task is still announced;
#   (c) a held task's timed read that times out is not EOF on bash 3.2, whose
#       `read -t` returns 1 for both: the watcher is still there once the timeout
#       passed. The watcher runs under /bin/bash when that is 3.2 (macOS); with
#       no bash 3.2 (c) is SKIPPED and (c') runs the same steps on this host's
#       bash, where a timeout returns >128 and must not end the watcher either;
#   (d) cleanup signals children, not a process group it does not lead: the
#       wrapper survives the watcher's plain SIGTERM exit and records the real
#       status, 0, never 144.
# Run: bash tests/watch-tasks-stream-survives-sigurg-and-fswatch-death.test.sh
set -u -m
# `-m` (job control) gives the backgrounded wrapper its own process group, with
# the wrapper as its leader, exactly the shape the Monitor's `zsh -c` gives it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
# (c) needs bash 3.2's `read -t`: /bin/bash when that is 3.x, the interpreter the
# notifier execs its standby under; PATH's bash (5 on Homebrew and CI) cannot show it.
WATCHER_BASH=bash
[ "$(/bin/bash -c 'echo "${BASH_VERSINFO[0]}"' 2>/dev/null)" = 3 ] && WATCHER_BASH=/bin/bash
WATCHER_BASH_VERSION="$("$WATCHER_BASH" -c 'echo "$BASH_VERSION"')"

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-urg.XXXXXX")"
mkdir -p "$WORK/tasks" "$WORK/state" "$WORK/results"

# Real events on any host: a polling stand-in for fswatch.
STUBBIN="$WORK/stubbin"
mkdir -p "$STUBBIN"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"
chmod +x "$STUBBIN/fswatch"
export PATH="$STUBBIN:$PATH"

WRAPPER="$WORK/wrapper.sh"
cat > "$WRAPPER" <<EOS
#!/bin/bash
"$WATCHER_BASH" "$WATCHER" "$WORK/tasks" --role session --inbox "$WORK/tasks" > "$WORK/out.log" 2> "$WORK/err.log"
echo "watcher rc=\$?" > "$WORK/wrapper.rc"
EOS

wrapper_pid=""
cleanup_all() {
  [ -n "$wrapper_pid" ] && { kill -9 -- "-$wrapper_pid" 2>/dev/null || kill -9 "$wrapper_pid" 2>/dev/null || true; }
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

alive() { kill -0 "$1" 2>/dev/null; }
fswatch_of() { pgrep -P "$1" -f fswatch 2>/dev/null | head -1; }
announced() {  # announced <name> <tenths>: TASK_FILE line for <name> within the wait
  local i
  for i in $(seq 1 "$2"); do
    grep -q "^TASK_FILE: $1\$" "$WORK/out.log" 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}

env -u SUTANDO_INSTANCE_ID SUTANDO_WORKSPACE_DIR="$WORK" SUTANDO_STANDBY_STOP_TIMEOUT=1 \
  SUTANDO_HELD_RETRY_INTERVAL=2 SUTANDO_FSWATCH_RESTART_MAX=5 \
  bash "$WRAPPER" &
wrapper_pid=$!

watcher_pid=""
for i in $(seq 1 150); do
  watcher_pid="$(pgrep -P "$wrapper_pid" 2>/dev/null | head -1)"
  [ -n "$watcher_pid" ] && [ "$(cat "$WORK"/state/*.pid 2>/dev/null | head -1)" = "$watcher_pid" ] && break
  watcher_pid=""
  sleep 0.1
done
if [ -z "$watcher_pid" ]; then
  echo "  FAIL: setup -- the session watcher never stamped its sentinel; stderr: $(tail -3 "$WORK/err.log" 2>/dev/null)"
  fail=1
fi
if [ "$fail" -eq 0 ] && [ "$(ps -o pgid= -p "$watcher_pid" | tr -d ' ')" != "$wrapper_pid" ]; then
  echo "  FAIL: setup -- the watcher ($watcher_pid) is not in the wrapper's group ($wrapper_pid), so this is not the Monitor's shape"
  fail=1
fi
if [ "$fail" -eq 0 ]; then
  case "$(ps -o command= -p "$watcher_pid")" in
    "$WATCHER_BASH $WATCHER "*) ;;
    *) echo "  FAIL: setup -- the watcher is not running under $WATCHER_BASH: $(ps -o command= -p "$watcher_pid")"; fail=1 ;;
  esac
fi

if [ "$fail" -eq 0 ]; then
  fsw="$(fswatch_of "$watcher_pid")"
  echo "task one" > "$WORK/tasks/task-1.txt"
  announced task-1.txt 50 && echo "  PASS: setup -- task-1 announced by watcher $watcher_pid (fswatch $fsw) under $WATCHER_BASH $WATCHER_BASH_VERSION" \
    || { echo "  FAIL: setup -- task-1 never announced; stderr: $(tail -3 "$WORK/err.log")"; fail=1; }
fi

if [ "$fail" -eq 0 ]; then
  # --- (a): SIGURG is nothing ---------------------------------------------------
  kill -URG "$watcher_pid" 2>/dev/null
  [ -n "$fsw" ] && kill -URG "$fsw" 2>/dev/null
  sleep 0.5
  echo "task two" > "$WORK/tasks/task-2.txt"
  if alive "$watcher_pid" && { [ -z "$fsw" ] || alive "$fsw"; } && announced task-2.txt 50; then
    echo "  PASS (a): SIGURG to the watcher and its fswatch: both alive, task-2 announced"
  else
    echo "  FAIL (a): after SIGURG: watcher-alive=$(alive "$watcher_pid" && echo yes || echo no) fswatch-alive=$(alive "${fsw:-0}" && echo yes || echo no) announced=$(grep -c '^TASK_FILE: task-2.txt$' "$WORK/out.log" 2>/dev/null); wrapper: $(cat "$WORK/wrapper.rc" 2>/dev/null)"
    fail=1
  fi

  # --- (b): fswatch dies, the watcher does not ---------------------------------
  fsw="$(fswatch_of "$watcher_pid")"
  [ -n "$fsw" ] && kill -9 "$fsw" 2>/dev/null
  new_fsw=""
  for i in $(seq 1 80); do
    new_fsw="$(fswatch_of "$watcher_pid")"
    [ -n "$new_fsw" ] && [ "$new_fsw" != "$fsw" ] && break
    new_fsw=""
    sleep 0.1
  done
  echo "task three" > "$WORK/tasks/task-3.txt"
  if alive "$watcher_pid" && [ -n "$new_fsw" ] && grep -q "restarting it in" "$WORK/err.log" && announced task-3.txt 80; then
    echo "  PASS (b): fswatch $fsw killed: relaunched as $new_fsw, the watcher stayed, task-3 announced"
  else
    echo "  FAIL (b): after fswatch died: watcher-alive=$(alive "$watcher_pid" && echo yes || echo no) new-fswatch='${new_fsw}' announced=$(grep -c '^TASK_FILE: task-3.txt$' "$WORK/out.log" 2>/dev/null); stderr: $(tail -3 "$WORK/err.log"); wrapper: $(cat "$WORK/wrapper.rc" 2>/dev/null)"
    fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  # --- (c): a held task's timed read ---------------------------------------------
  # A standing claim with no handler here holds the task, so the loop's read is
  # timed (SUTANDO_HELD_RETRY_INTERVAL=2); two timeouts pass before it is lifted.
  mkdir -p "$WORK/state/task-event-handler-claims"
  printf '1\nsomebody\n%s\nfallback\n\n' "$WORK/tasks/task-4.txt" > "$WORK/state/task-event-handler-claims/task-4.txt"
  echo "task four" > "$WORK/tasks/task-4.txt"
  held=""
  for i in $(seq 1 50); do
    grep -q "holding task-4.txt" "$WORK/err.log" 2>/dev/null && { held=1; break; }
    sleep 0.1
  done
  sleep 4   # two timeouts' worth
  rm -f "$WORK/state/task-event-handler-claims/task-4.txt"
  echo "task five" > "$WORK/tasks/task-5.txt"
  ok=""
  [ -n "$held" ] && alive "$watcher_pid" && announced task-5.txt 50 && ok=1
  detail="held=${held:-0} watcher-alive=$(alive "$watcher_pid" && echo yes || echo no) announced=$(grep -c '^TASK_FILE: task-5.txt$' "$WORK/out.log" 2>/dev/null); wrapper: $(cat "$WORK/wrapper.rc" 2>/dev/null)"
  case "$WATCHER_BASH_VERSION" in
    3.*) check=c; timeout_status="1, as EOF's" ;;
    *) echo "  SKIP (c): no bash 3.2 on this host"; check="c'"; timeout_status=">128" ;;
  esac
  if [ -n "$ok" ]; then
    echo "  PASS ($check): under bash $WATCHER_BASH_VERSION a held task's read timed out (status $timeout_status): the watcher stayed, task-5 announced"
  else
    echo "  FAIL ($check): under bash $WATCHER_BASH_VERSION: $detail"
    fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  # --- (d): a plain SIGTERM exit leaves the wrapper to report it ----------------
  kill -TERM "$watcher_pid" 2>/dev/null
  for i in $(seq 1 100); do
    alive "$wrapper_pid" || break
    sleep 0.1
  done
  wait "$wrapper_pid" 2>/dev/null
  wrapper_pid=""
  rc_line="$(cat "$WORK/wrapper.rc" 2>/dev/null)"
  if [ "$rc_line" = "watcher rc=0" ] && [ -z "$(ls "$WORK"/state/*.pid 2>/dev/null)" ]; then
    echo "  PASS (d): the wrapper outlived the watcher's SIGTERM exit and saw its real status ($rc_line); sentinel released"
  else
    echo "  FAIL (d): wrapper record '${rc_line:-<none: the wrapper was killed with the group>}', sentinels left: $(ls "$WORK"/state/*.pid 2>/dev/null | wc -l | tr -d ' ')"
    fail=1
  fi
  if pgrep -f "fswatch.*$WORK" >/dev/null 2>&1; then
    echo "  FAIL (d'): an fswatch outlived its watcher"
    fail=1
  else
    echo "  PASS (d'): no fswatch outlived its watcher"
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "PASSED: the session watcher (bash $WATCHER_BASH_VERSION) survives SIGURG, an fswatch death and a held read's timeout, and its exit is reported as its own"
else
  echo "FAILED: the session watcher (bash $WATCHER_BASH_VERSION) survives SIGURG, an fswatch death and a held read's timeout, and its exit is reported as its own"
fi
exit "$fail"

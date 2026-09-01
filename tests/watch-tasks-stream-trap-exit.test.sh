#!/usr/bin/env bash
# Regression guard: watch-tasks-stream.sh's cleanup() trap must actually
# terminate the process on SIGTERM/SIGHUP/SIGINT, not just run cleanup and
# let the script resume.
#
# Found 2026-07-01: `trap cleanup EXIT HUP INT TERM` ran `rm -f "$PID_FILE";
# kill 0` on receipt of SIGTERM, but never called `exit` — a trap only
# overrides a signal's default disposition, it doesn't terminate the process
# by itself. Plain `kill <pid>` against a running watcher left the process
# alive; only `kill -9` actually stopped it. Fix: HUP/INT/TERM now explicitly
# `exit 0` after cleanup.
#
# Runs under CI (the shell-standalone-tests step) and manually via
# `bash tests/watch-tasks-stream-trap-exit.test.sh`.

set -u -m
# `-m` (job control) gives the backgrounded harness its own process group, so
# the extracted `kill 0` (send to caller's own group) only reaps the harness
# — not this test script's process group. Without `-m`, a non-interactive
# bash puts background jobs in the SAME group as the script, and `kill 0`
# takes the test runner down with it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"

fail=0

# Structural: HUP/INT/TERM trap must call exit, not just cleanup.
grep -Eq "trap '?cleanup; *exit 0'? HUP INT TERM" "$WATCHER" \
    || { echo "  FAIL: HUP/INT/TERM trap doesn't explicitly exit after cleanup"; fail=1; }

# Behavioral: extract the ACTUAL cleanup()/trap lines from the real script
# (not a hand-copied reimplementation) and exercise them in full isolation —
# a throwaway PID file, no real workspace touched, no dependency on
# sutando-config.sh resolution.
TMPDIR_T="$(mktemp -d)"
PID_FILE="$TMPDIR_T/watcher.pid"
HARNESS="$TMPDIR_T/harness.sh"

{
  echo '#!/usr/bin/env bash'
  echo "PID_FILE=\"$PID_FILE\""
  echo 'echo "$$" > "$PID_FILE"'
  # cleanup() delegates sentinel removal to the shared ownership helper, which
  # the real script sources at its top. The harness re-creates the script's
  # CONTEXT, so it must source it too — otherwise the extracted lines run with
  # the function undefined and the file is never removed, which reads as the
  # trap being broken rather than the harness being incomplete.
  echo "source \"$REPO/src/watcher_sentinel.sh\""
  # Pull the exact cleanup()/trap lines out of the real script so this test
  # breaks if the fix is ever reverted or edited incompatibly.
  sed -n '/^cleanup()/,/^trap .*HUP INT TERM$/p' "$WATCHER"
  # `read` (a bash builtin) blocks until input/EOF/signal and is interrupted
  # IMMEDIATELY on a trapped signal per bash's documented behavior. A `sleep`
  # loop instead waits on an EXTERNAL process each iteration — some
  # bash/coreutils combinations defer running a pending trap until that
  # foreground child exits, which is the likely source of this test's CI
  # flake (needed the watchdog's SIGKILL there, passed instantly locally).
  # The real watch-tasks-stream.sh blocks on fswatch, not a sleep loop
  # anyway — this placeholder only needs to keep the harness alive, so its
  # exact mechanism doesn't need to mirror production.
  echo 'while true; do read -r -t 3600 _ 2>/dev/null || true; done'
} > "$HARNESS"

bash "$HARNESS" &
HARNESS_PID=$!
sleep 1

if [ ! -f "$PID_FILE" ]; then
  echo "  FAIL: harness never wrote its PID file — setup broken"
  fail=1
else
  # Poll for death rather than inferring it from a single elapsed-time
  # window: $SECONDS has 1s granularity and a scheduler-contended CI
  # runner can legitimately take longer than a quiet local box to reap a
  # process, so a fixed "elapsed < Ns" check risked a false FAIL under
  # load without indicating the fix itself is broken. Track explicitly
  # whether SIGKILL was needed via a sentinel the watchdog writes right
  # before firing — that's the actual thing under test, not a timing proxy
  # for it. Generous 8s poll window before the watchdog's SIGKILL; still
  # correctly fails if the plain SIGTERM is truly never honored.
  NEEDED_SIGKILL="$TMPDIR_T/needed-sigkill"
  ( sleep 8; touch "$NEEDED_SIGKILL"; kill -9 "$HARNESS_PID" 2>/dev/null ) &
  WATCHDOG_PID=$!
  kill "$HARNESS_PID" 2>/dev/null
  for _ in $(seq 1 80); do  # 8s @ 100ms
    kill -0 "$HARNESS_PID" 2>/dev/null || break
    sleep 0.1
  done
  wait "$HARNESS_PID" 2>/dev/null
  kill "$WATCHDOG_PID" 2>/dev/null
  wait "$WATCHDOG_PID" 2>/dev/null

  if [ ! -f "$NEEDED_SIGKILL" ]; then
    echo "  PASS: watcher terminated on plain SIGTERM"
  else
    echo "  FAIL: watcher still alive after plain SIGTERM — needed SIGKILL (pid $HARNESS_PID)"
    fail=1
  fi
  if [ -f "$PID_FILE" ]; then
    echo "  FAIL: PID file not removed after termination"
    fail=1
  else
    echo "  PASS: PID file removed after termination"
  fi
fi

rm -rf "$TMPDIR_T"

if [ "$fail" -eq 0 ]; then
  echo "PASSED: watch-tasks-stream trap-exit fix"
else
  echo "FAILED: watch-tasks-stream trap-exit fix"
fi
exit "$fail"

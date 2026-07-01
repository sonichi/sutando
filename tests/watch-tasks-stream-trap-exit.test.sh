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
# Not run under CI yet (`.test.sh` isn't currently wired into
# .github/workflows/ci.yml — a pre-existing gap, out of scope here); run
# manually via `bash tests/watch-tasks-stream-trap-exit.test.sh`.

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
  # Pull the exact cleanup()/trap lines out of the real script so this test
  # breaks if the fix is ever reverted or edited incompatibly.
  sed -n '/^cleanup()/,/^trap .*HUP INT TERM$/p' "$WATCHER"
  echo 'while true; do sleep 1; done'
} > "$HARNESS"

bash "$HARNESS" &
HARNESS_PID=$!
sleep 1

if [ ! -f "$PID_FILE" ]; then
  echo "  FAIL: harness never wrote its PID file — setup broken"
  fail=1
else
  # Bounded wait, not a bare blocking `wait` — if the fix regresses, the
  # harness never exits on its own and `wait` would hang the test forever.
  # A watchdog force-kills it after 3s so `wait` always returns; elapsed
  # time then tells us whether the PLAIN kill (not the watchdog's SIGKILL)
  # is what actually stopped it. `wait` (true reap) also avoids the
  # zombie false negative a bare `kill -0` would give immediately after
  # a successful plain kill.
  ( sleep 3; kill -9 "$HARNESS_PID" 2>/dev/null ) &
  WATCHDOG_PID=$!
  start_ts=$SECONDS
  kill "$HARNESS_PID" 2>/dev/null
  wait "$HARNESS_PID" 2>/dev/null
  elapsed=$((SECONDS - start_ts))
  kill "$WATCHDOG_PID" 2>/dev/null
  wait "$WATCHDOG_PID" 2>/dev/null

  if [ "$elapsed" -lt 3 ]; then
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

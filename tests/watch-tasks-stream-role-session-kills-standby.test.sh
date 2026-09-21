#!/usr/bin/env bash
# Regression guard for #4477 item (c): a watcher started with `--role session`
# must kill the standby tmux session ("${SUTANDO_TMUX_SESSION}-watcher") for
# its OWN inbox at its own startup, in code -- redundant with (belt-and-
# suspenders to) task-notifier-supervisor.sh's own poll-and-stop, never an
# agent instruction. Also proves the sentinel ends up owned solely by the
# arming internal watcher, not corrupted by the handoff.
#
# Run: bash tests/watch-tasks-stream-role-session-kills-standby.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-role-kill.XXXXXX")"
SOCK="$WORK/tmux.sock"
mkdir -p "$WORK/tasks" "$WORK/state"

cleanup_all() {
  tmux -S "$SOCK" kill-server >/dev/null 2>&1 || true
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

# Standby: an external watcher for THIS inbox, named the way the supervisor's
# launcher names it -- "${SUTANDO_TMUX_SESSION}-watcher".
tmux -S "$SOCK" new-session -d -s "witness-watcher" -c "$REPO" \
  "env -u SUTANDO_INSTANCE_ID SUTANDO_WORKSPACE_DIR=$WORK bash $WATCHER $WORK/tasks > $WORK/standby.log 2>&1"

for i in $(seq 1 50); do
  [ -f "$WORK"/state/*.pid ] 2>/dev/null && break
  ls "$WORK"/state/*.pid >/dev/null 2>&1 && break
  sleep 0.1
done

if ! tmux -S "$SOCK" has-session -t witness-watcher 2>/dev/null; then
  echo "  FAIL: setup -- standby session never came up"
  fail=1
else
  # Internal (in-session) watcher arms for the SAME inbox, same tmux socket
  # and session name the standby was derived from. SUTANDO_WORKSPACE_DIR set
  # explicitly, matching what both real launchers always forward -- without
  # it here the dirname-fallback resolution path stalls on this host, a
  # pre-existing gap this test isn't about and doesn't need to chase.
  tmux -S "$SOCK" new-session -d -s "witness-internal" -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID SUTANDO_WORKSPACE_DIR=$WORK SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=witness \
       bash $WATCHER $WORK/tasks --role session --inbox $WORK/tasks > $WORK/internal.log 2>&1"

  standby_gone=""
  for i in $(seq 1 50); do
    tmux -S "$SOCK" has-session -t witness-watcher 2>/dev/null || { standby_gone=1; break; }
    sleep 0.1
  done

  if [ -n "$standby_gone" ]; then
    echo "  PASS: standby session killed by the internal watcher's own startup code"
  else
    echo "  FAIL: standby session still alive after the internal watcher armed"
    fail=1
  fi

  if tmux -S "$SOCK" has-session -t witness-internal 2>/dev/null; then
    echo "  PASS: the arming internal watcher itself is unaffected (still running)"
  else
    echo "  FAIL: the internal watcher did not survive its own startup"
    fail=1
  fi

  sleep 0.3
  sentinel_count=$(ls "$WORK"/state/*.pid 2>/dev/null | wc -l | tr -d ' ')
  if [ "$sentinel_count" = "1" ]; then
    echo "  PASS: exactly one sentinel remains after the handoff (no corruption)"
  else
    echo "  FAIL: expected exactly 1 sentinel after the handoff, found $sentinel_count"
    fail=1
  fi

  grep -q "role=session inbox=$WORK/tasks" "$WORK/internal.log" 2>/dev/null \
    && echo "  PASS: role/inbox announced to stderr as expected" \
    || { echo "  FAIL: role/inbox announce line missing from internal watcher's stderr"; fail=1; }
fi

if [ "$fail" -eq 0 ]; then
  echo "PASSED: watch-tasks-stream --role session kills its own standby"
else
  echo "FAILED: watch-tasks-stream --role session kills its own standby"
fi
exit "$fail"

#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session lives.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
RESTART_DELAY="${SUTANDO_NOTIFIER_RESTART_DELAY:-1}"
NOTIFIER="${SUTANDO_NOTIFIER_SCRIPT:-$REPO/src/agent/codex/cli/task-notifier.sh}"
child_pid=""

stop_child() {
  [ -n "$child_pid" ] || return 0
  # The Python child calls setsid(), so its PID is also the notifier process
  # group's ID. Stop the whole group; fall back to the leader during the tiny
  # pre-setsid race.
  kill -TERM "-$child_pid" 2>/dev/null || kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  child_pid=""
}

trap 'stop_child; exit 0' HUP INT TERM

while tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; do
  # watch-tasks-stream.sh deliberately uses `kill 0` when its fswatch pipeline
  # ends so no orphan child survives. Run the notifier in a separate process
  # group; otherwise that cleanup signal also kills this supervisor and tmux
  # removes the entire watcher session—the production failure fixed here.
  python3 -c \
    'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1]])' \
    "$NOTIFIER" &
  child_pid=$!
  wait "$child_pid"
  status=$?
  child_pid=""
  tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null || exit 0
  echo "task-notifier-supervisor: notifier exited with status $status; restarting" >&2
  sleep "$RESTART_DELAY"
done

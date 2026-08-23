#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session lives.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
RESTART_DELAY="${SUTANDO_NOTIFIER_RESTART_DELAY:-1}"
RESTART_DELAY_MAX="${SUTANDO_NOTIFIER_RESTART_DELAY_MAX:-30}"
STABLE_AFTER="${SUTANDO_NOTIFIER_STABLE_AFTER:-60}"
LOCK_DIR="${SUTANDO_NOTIFIER_LOCK_DIR:-${TMPDIR:-/tmp}/sutando-notifier-${SESSION}.lock}"
NOTIFIER="${SUTANDO_NOTIFIER_SCRIPT:-$REPO/src/agent/codex/cli/task-notifier.sh}"
# task-notifier.sh exits 2 only for a usage/configuration fault; respawning
# re-runs the same broken invocation, so that one is terminal rather than retried.
FATAL_STATUS=2
child_pid=""
lease_held=""

stop_child() {
  [ -n "$child_pid" ] || return 0
  # The Python child calls setsid(), so its PID is also the notifier process
  # group's ID. Stop the whole group; fall back to the leader during the tiny
  # pre-setsid race.
  kill -TERM "-$child_pid" 2>/dev/null || kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  child_pid=""
}

release_lease() {
  [ -n "$lease_held" ] || return 0
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  lease_held=""
}

# mkdir is the portable atomic test-and-set; macOS ships no flock(1).
acquire_lease() {
  local attempt owner
  for attempt in 1 2 3; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      lease_held=1
      return 0
    fi
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
      echo "task-notifier-supervisor: pid $owner already supervises '$SESSION'; exiting" >&2
      exit 0
    fi
    # Owner is gone: the lease is stale, not contended. Clear it and retry.
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  done
  echo "task-notifier-supervisor: could not acquire lease $LOCK_DIR" >&2
  exit 1
}

trap 'stop_child; release_lease; exit 0' HUP INT TERM
trap 'release_lease' EXIT

acquire_lease

delay="$RESTART_DELAY"
while tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; do
  started_at="$(date +%s)"
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
  if [ "$status" -eq "$FATAL_STATUS" ]; then
    echo "task-notifier-supervisor: notifier exited with status $status (configuration fault); not restarting" >&2
    exit "$status"
  fi
  # A run that lasted proves the fault cleared; a short one is a crash loop, so
  # only the former resets the backoff.
  if [ "$(( $(date +%s) - started_at ))" -ge "$STABLE_AFTER" ]; then
    delay="$RESTART_DELAY"
  fi
  echo "task-notifier-supervisor: notifier exited with status $status; restarting in ${delay}s" >&2
  sleep "$delay"
  delay=$(( delay * 2 ))
  [ "$delay" -gt "$RESTART_DELAY_MAX" ] && delay="$RESTART_DELAY_MAX"
done

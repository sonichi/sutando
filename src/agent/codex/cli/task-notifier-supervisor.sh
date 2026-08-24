#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session lives.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
RESTART_DELAY="${SUTANDO_NOTIFIER_RESTART_DELAY:-1}"
RESTART_DELAY_MAX="${SUTANDO_NOTIFIER_RESTART_DELAY_MAX:-30}"
STABLE_AFTER="${SUTANDO_NOTIFIER_STABLE_AFTER:-60}"
# start-cli.sh launches one notifier per (socket, session); keying on SESSION
# alone makes two cores on different sockets suppress each other.
_lease_key() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }
LOCK_DIR="${SUTANDO_NOTIFIER_LOCK_DIR:-${TMPDIR:-/tmp}/sutando-notifier-$(_lease_key "${TMUX_SOCKET}#${SESSION}").lock}"
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

# Our token: pid plus this process's start identity, so a recycled pid cannot
# impersonate us. Read once — /proc is absent on macOS, so ps is the portable source.
_own_token() {
  printf '%s:%s' "$$" "$(ps -o lstart= -p "$$" 2>/dev/null | tr -s ' ' '_' || echo unknown)"
}
OWNER_TOKEN="$(_own_token)"

_lease_token() { cat "$LOCK_DIR/token" 2>/dev/null || true; }

# Non-recursive by construction: remove the files we know we wrote, then rmdir,
# which FAILS if anything else is inside. `rm -rf` on a configurable path can
# delete a pre-existing directory and its contents.
_lease_rm() {
  rm -f "$LOCK_DIR/token" "$LOCK_DIR/pid" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

release_lease() {
  [ -n "$lease_held" ] || return 0
  # Verify the path still holds OUR lease. Without this a former owner whose
  # lease was already reclaimed deletes its successor's.
  if [ "$(_lease_token)" = "$OWNER_TOKEN" ]; then
    _lease_rm
  fi
  lease_held=""
}

PUBLISH_GRACE="${SUTANDO_NOTIFIER_PUBLISH_GRACE:-10}"

_pid_token() { printf '%s:%s' "$1" "$(ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' '_' || echo unknown)"; }

_lease_age() {
  local now mt
  now="$(date +%s)"
  mt="$(stat -f %m "$LOCK_DIR" 2>/dev/null || stat -c %Y "$LOCK_DIR" 2>/dev/null || echo "$now")"
  echo $(( now - mt ))
}

# mkdir is the portable atomic test-and-set; macOS ships no flock(1).
acquire_lease() {
  local attempt owner token age
  for attempt in 1 2 3; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      # Publish via rename so a contender sees the token whole or not at all.
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      printf '%s' "$OWNER_TOKEN" > "$LOCK_DIR/.token.$$" \
        && mv -f "$LOCK_DIR/.token.$$" "$LOCK_DIR/token"
      lease_held=1
      return 0
    fi
    token="$(_lease_token)"
    owner="${token%%:*}"
    if [ -z "$token" ]; then
      # No token. A `pid` file is still usable identity (a pre-token lease), so
      # fall through to the liveness check rather than widening the grace window.
      owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    fi
    if [ -z "$owner" ]; then
      # Neither token nor pid: the ONLY genuinely ambiguous state — a winner
      # between mkdir and publication, or a crash inside it. Bounded, and it
      # defers, because deleting here races a live owner into a double-run.
      age="$(_lease_age)"
      if [ "$age" -lt "$PUBLISH_GRACE" ]; then
        echo "task-notifier-supervisor: lease $LOCK_DIR is publishing (${age}s); exiting" >&2
        exit 0
      fi
      _lease_rm
      continue
    fi
    # A token pins the START identity too, so a recycled pid cannot impersonate
    # the owner. A pre-token lease has only the pid to go on.
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null \
       && { [ -z "$token" ] || [ "$(_pid_token "$owner")" = "$token" ]; }; then
      echo "task-notifier-supervisor: pid $owner already supervises '$SESSION'; exiting" >&2
      exit 0
    fi
    # Owner is gone (or its pid was recycled by an unrelated process): stale.
    _lease_rm
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
  # awk, not $(( )): bash arithmetic is integer-only and the delay is
  # documented as fractional -- tests drive it at 0.01 to stay fast.
  delay="$(awk -v d="$delay" -v m="$RESTART_DELAY_MAX" \
    'BEGIN { d *= 2; if (d > m) d = m; print d }')"
done

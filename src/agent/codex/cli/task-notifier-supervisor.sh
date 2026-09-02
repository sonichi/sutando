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
# Readable session prefix plus a digest of the exact (socket, session) pair:
# flattening punctuation to `_` made distinct sockets share one lease.
_lease_key() {
  printf '%s-%s' "$(printf '%s' "$2" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)" \
    "$(python3 -c 'import hashlib,sys;print(hashlib.sha256("\0".join(sys.argv[1:]).encode()).hexdigest()[:16])' "$1" "$2")"
}
LOCK_DIR="${SUTANDO_NOTIFIER_LOCK_DIR:-${TMPDIR:-/tmp}/sutando-notifier-$(_lease_key "$TMUX_SOCKET" "$SESSION").lock}"
RECLAIM_DIR="$LOCK_DIR.reclaim"
RECLAIM_HOOK="${SUTANDO_NOTIFIER_RECLAIM_HOOK:-}"
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
# Prints the start identity of pid $1; fails when it cannot be measured. An
# empty `ps` piped through `tr` is not an identity, it is a blind probe.
_start_of() {
  local out
  out="$(ps -o lstart= -p "$1" 2>/dev/null)" || return 1
  [ -n "$out" ] || return 1
  printf '%s' "$out" | tr -s ' ' '_'
}
_own_token() { printf '%s:%s' "$$" "$(_start_of "$$" || echo unknown)"; }
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

# Verdict on the lease at LOCK_DIR: live | publishing | unknown | stale.
# `unknown` is a live pid whose start identity cannot be compared on either
# side; it must read as contended, never as pid reuse.
_judge() {
  local token owner age start
  token="$(_lease_token)"
  owner="${token%%:*}"
  [ -n "$token" ] || owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -z "$owner" ]; then
    age="$(_lease_age)"
    [ "$age" -lt "$PUBLISH_GRACE" ] && { echo publishing; return; }
    echo stale; return
  fi
  kill -0 "$owner" 2>/dev/null || { echo stale; return; }
  [ -n "$token" ] || { echo live; return; }
  start="$(_start_of "$owner" || true)"
  if [ -z "$start" ] || [ "${token#*:}" = unknown ]; then echo unknown; return; fi
  [ "$start" = "${token#*:}" ] && echo live || echo stale
}

_reclaim_unlock() { rm -f "$RECLAIM_DIR/pid" 2>/dev/null; rmdir "$RECLAIM_DIR" 2>/dev/null || true; }

# Only the holder of RECLAIM_DIR may delete a lease, and only on a verdict taken
# under that lock: a verdict from before the lock is a read another reclaimer
# may already have acted on.
_reclaim_stale() {
  local rpid
  if ! mkdir "$RECLAIM_DIR" 2>/dev/null; then
    rpid="$(cat "$RECLAIM_DIR/pid" 2>/dev/null || true)"
    if [ -n "$rpid" ] && kill -0 "$rpid" 2>/dev/null; then return 1; fi
    _reclaim_unlock
    return 1
  fi
  printf '%s\n' "$$" > "$RECLAIM_DIR/pid"
  if [ -d "$LOCK_DIR" ] && [ "$(_judge)" = stale ]; then
    _lease_rm
    _reclaim_unlock
    return 0
  fi
  _reclaim_unlock
  return 1
}

_lease_age() {
  local now mt
  now="$(date +%s)"
  # GNU first and validate: `stat -f` on GNU is --file-system and SUCCEEDS with
  # non-numeric output, so a BSD-first probe never falls through on Linux.
  mt="$(stat -c %Y "$LOCK_DIR" 2>/dev/null || true)"
  case "$mt" in '' | *[!0-9]*) mt="$(stat -f %m "$LOCK_DIR" 2>/dev/null || true)" ;; esac
  # Unreadable mtime reads as just-published, so the guard defers rather than
  # reclaiming: an unknown age must never authorise deleting a live lease.
  case "$mt" in '' | *[!0-9]*) mt="$now" ;; esac
  echo $(( now - mt ))
}

# mkdir is the portable atomic test-and-set; macOS ships no flock(1).
acquire_lease() {
  local attempt verdict
  for attempt in 1 2 3 4 5 6; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      # Publish via rename so a contender sees the token whole or not at all.
      printf '%s\n' "$$" > "$LOCK_DIR/pid"
      printf '%s' "$OWNER_TOKEN" > "$LOCK_DIR/.token.$$" \
        && mv -f "$LOCK_DIR/.token.$$" "$LOCK_DIR/token"
      lease_held=1
      return 0
    fi
    verdict="$(_judge)"
    case "$verdict" in
      live)
        echo "task-notifier-supervisor: pid $(_lease_token | cut -d: -f1) already supervises '$SESSION'; exiting" >&2
        exit 0 ;;
      publishing)
        echo "task-notifier-supervisor: lease $LOCK_DIR is publishing ($(_lease_age)s); exiting" >&2
        exit 0 ;;
      unknown)
        echo "task-notifier-supervisor: cannot verify the start identity of lease holder $(_lease_token | cut -d: -f1); assuming live; exiting" >&2
        exit 0 ;;
    esac
    # Stale by a read outside the lock. The hook is a test seam that lets a
    # control hold this reclaimer exactly here.
    [ -n "$RECLAIM_HOOK" ] && bash "$RECLAIM_HOOK"
    _reclaim_stale || sleep 0.2
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

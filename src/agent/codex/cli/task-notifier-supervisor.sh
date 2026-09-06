#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session lives.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
# shellcheck source=src/portable_mtime.sh
. "$REPO/src/portable_mtime.sh"
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
  local digest
  digest="$(python3 -c 'import hashlib,sys;print(hashlib.sha256("\0".join(sys.argv[1:]).encode()).hexdigest()[:16])' "$1" "$2" 2>/dev/null)" || digest=""
  # An empty digest aliases every socket onto one key: fail closed, never lease.
  # Malformed successful output (sixteen non-hex bytes) also aliases sockets.
  if ! [[ "$digest" =~ ^[0-9a-f]{16}$ ]]; then
    echo "task-notifier-supervisor: cannot derive the lease key for ($1, $2); refusing to run" >&2; exit 1
  fi
  printf '%s-%s' "$(printf '%s' "$2" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-40)" "$digest"
}
if [ -n "${SUTANDO_NOTIFIER_LOCK_DIR:-}" ]; then
  LOCK_DIR="$SUTANDO_NOTIFIER_LOCK_DIR"
else
  LOCK_KEY="$(_lease_key "$TMUX_SOCKET" "$SESSION")" || exit 1
  LOCK_DIR="${TMPDIR:-/tmp}/sutando-notifier-$LOCK_KEY.lock"
fi
RECLAIM_DIR="$LOCK_DIR.reclaim"
RECLAIM_HOOK="${SUTANDO_NOTIFIER_RECLAIM_HOOK:-}"
RECLAIM_PUBLISH_HOOK="${SUTANDO_NOTIFIER_RECLAIM_PUBLISH_HOOK:-}"
JUDGE_HOOK="${SUTANDO_NOTIFIER_JUDGE_HOOK:-}"
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

PUBLISH_GRACE="${SUTANDO_NOTIFIER_PUBLISH_GRACE:-10}"

_token_of() { cat "$1/token" 2>/dev/null || true; }
_lease_token() { _token_of "$LOCK_DIR"; }

_age_of() {
  local now mt
  now="$(date +%s)"
  # Unreadable mtime reads as just-published, so the guard defers rather than
  # reclaiming: an unknown age must never authorise deleting a live lease.
  mt="$(portable_mtime "$1")" || mt="$now"
  echo $(( now - mt ))
}
_lease_age() { _age_of "$LOCK_DIR"; }

# Verdict on the lease directory $1: live | publishing | unknown | stale |
# absent | error. `unknown` is a live pid whose start identity cannot be
# compared on either side; it must read as contended, never as pid reuse.
# `absent` is a directory that vanished between mkdir and this read (retry);
# `error` is a parent that cannot hold it at all (a real path/config fault).
_judge_dir() {
  local dir="$1" token owner age start parent
  if [ ! -d "$dir" ]; then
    parent="$(dirname "$dir")"
    if [ -d "$parent" ] && [ -w "$parent" ]; then echo absent; else echo error; fi
    return
  fi
  token="$(_token_of "$dir")"
  owner="${token%%:*}"
  [ -n "$token" ] || owner="$(cat "$dir/pid" 2>/dev/null || true)"
  if [ -z "$owner" ]; then
    age="$(_age_of "$dir")"
    [ "$age" -lt "$PUBLISH_GRACE" ] && { echo publishing; return; }
    echo stale; return
  fi
  kill -0 "$owner" 2>/dev/null || { echo stale; return; }
  [ -n "$token" ] || { echo live; return; }
  start="$(_start_of "$owner" || true)"
  if [ -z "$start" ] || [ "${token#*:}" = unknown ]; then echo unknown; return; fi
  [ "$start" = "${token#*:}" ] && echo live || echo stale
}
_judge() { _judge_dir "$LOCK_DIR"; }

# Non-recursive by construction: remove the files we know we wrote, then rmdir,
# which FAILS if anything else is inside. `rm -rf` on a configurable path can
# delete a pre-existing directory and its contents. `.token.*` is the staging
# artifact an earlier revision of this script left inside the directory.
_dir_rm() {
  rm -f "$1/token" "$1/pid" "$1"/.token.* 2>/dev/null || true
  rmdir "$1" 2>/dev/null || true
}
_lease_rm() { _dir_rm "$LOCK_DIR"; }

# Publish a token into directory $1 whole-or-not-at-all: stage OUTSIDE the
# directory (a crash mid-publication must not leave a file that blocks rmdir),
# then link in. link(2) is atomic and refuses an existing name, so at most one
# publication ever lands in a directory: a publisher that lost the race to a
# post-grace claimant fails here and withdraws. Nonzero = did not land.
_publish_token() {
  local dir="$1" stage="$1.stage.$$"
  printf '%s' "$OWNER_TOKEN" > "$stage" || { rm -f "$stage"; return 1; }
  ln "$stage" "$dir/token" 2>/dev/null || { rm -f "$stage"; return 1; }
  rm -f "$stage"
  [ "$(_token_of "$dir")" = "$OWNER_TOKEN" ] || return 1
  printf '%s\n' "$$" > "$dir/pid" || return 1
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

# Release the reclaim lock only if it is still OURS by exact token.
_reclaim_release() {
  [ "$(_token_of "$RECLAIM_DIR")" = "$OWNER_TOKEN" ] && _dir_rm "$RECLAIM_DIR"
  return 0
}

# Take the reclaim lock: mkdir is the atomic test-and-set, but an empty
# directory is a publication in progress, not an abandoned one — it is judged
# by the same live/publishing/stale rule as the lease. A stale EMPTY directory
# is recovered by claiming it in place (the no-clobber link fences its late
# publisher out); a dead or recycled holder with a token is BROKEN by renaming
# its directory away (only one contender's mv succeeds), never deleted in place.
_reclaim_lock() {
  local verdict broken
  if mkdir "$RECLAIM_DIR" 2>/dev/null; then
    [ -n "$RECLAIM_PUBLISH_HOOK" ] && bash "$RECLAIM_PUBLISH_HOOK"
    _publish_token "$RECLAIM_DIR" && return 0
    _reclaim_release
    return 1
  fi
  verdict="$(_judge_dir "$RECLAIM_DIR")"
  case "$verdict" in
    stale)
      if [ -z "$(_token_of "$RECLAIM_DIR")" ]; then
        _publish_token "$RECLAIM_DIR" && return 0
        _reclaim_release
        return 1
      fi
      broken="$RECLAIM_DIR.broken.$$"
      mv "$RECLAIM_DIR" "$broken" 2>/dev/null && _dir_rm "$broken"
      ;;
  esac
  return 1
}

# Only the holder of RECLAIM_DIR may delete a lease, and only on a verdict taken
# under that lock: a verdict from before the lock is a read another reclaimer
# may already have acted on.
_reclaim_stale() {
  _reclaim_lock || return 1
  if [ -d "$LOCK_DIR" ] && [ "$(_judge)" = stale ]; then
    _lease_rm
    _reclaim_release
    return 0
  fi
  _reclaim_release
  return 1
}

# mkdir is the portable atomic test-and-set; macOS ships no flock(1).
acquire_lease() {
  local attempt verdict
  for attempt in 1 2 3 4 5 6; do
    if mkdir "$LOCK_DIR" 2>/dev/null; then
      if _publish_token "$LOCK_DIR"; then
        lease_held=1
        return 0
      fi
      echo "task-notifier-supervisor: could not publish the lease token in $LOCK_DIR" >&2
      _lease_rm
      exit 1
    fi
    [ -n "$JUDGE_HOOK" ] && bash "$JUDGE_HOOK"
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
      absent)
        # The holder released between our mkdir and this read: try again.
        sleep 0.1; continue ;;
      error)
        echo "task-notifier-supervisor: cannot create lease $LOCK_DIR (parent missing or unwritable)" >&2
        exit 1 ;;
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

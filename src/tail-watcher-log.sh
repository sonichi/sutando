#!/bin/bash
# Tail a detached watcher's event log from where THIS inbox's reader last got to,
# and record each line as it is emitted.
#
#   bash src/tail-watcher-log.sh --inbox <dir> [--workspace <dir>]
#
# The cursor lives in a file, not in the session's head: a re-arm happens exactly
# when a session cannot remember (a Monitor expiry, a compaction), and a cursor
# set to the log's current length skips every line between the two — the
# deliveries nothing else announces.
#
# The cursor file is also the READER's liveness: it is touched on every line and
# every poll, so a watcher whose reader has died is visible to the supervisor as
# a stale cursor, not as coverage. That is the property a detached watcher would
# otherwise take away: the watcher outlives its reader and hides the gap.
set -u

__SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
__REPO_ROOT="$(cd "$__SCRIPT_DIR/.." && pwd)"
# shellcheck source=tasks-dir-resolve.sh
source "$__SCRIPT_DIR/tasks-dir-resolve.sh"
# shellcheck source=../scripts/python-binary.sh
. "$__REPO_ROOT/scripts/python-binary.sh"

INBOX=""
WORKSPACE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --inbox) INBOX="${2:-}"; shift 2 ;;
    --inbox=*) INBOX="${1#--inbox=}"; shift ;;
    --workspace) WORKSPACE="${2:-}"; shift 2 ;;
    --workspace=*) WORKSPACE="${1#--workspace=}"; shift ;;
    *) echo "usage: tail-watcher-log.sh --inbox <dir> [--workspace <dir>]" >&2; exit 64 ;;
  esac
done
[ -n "$INBOX" ] || INBOX="${SUTANDO_TASKS_DIR:-}"
if [ -z "$INBOX" ]; then
  echo "tail-watcher-log: refusing to start: no --inbox and no SUTANDO_TASKS_DIR." >&2
  exit 64
fi
INBOX="$(canonical_tasks_dir "$INBOX")"
[ -n "$WORKSPACE" ] || WORKSPACE="${SUTANDO_WORKSPACE_DIR:-$(workspace_dir_for_inbox "$INBOX")}"
PY="$(require_python "$__REPO_ROOT" "tail the watcher log")" || exit 1

LOG="$("$PY" "$__REPO_ROOT/src/util_paths.py" watcher-log "$WORKSPACE" "$INBOX")" || exit 1
CURSOR="$("$PY" "$__REPO_ROOT/src/util_paths.py" watcher-log-cursor "$WORKSPACE" "$INBOX")" || exit 1
mkdir -p "$(dirname "$LOG")" "$(dirname "$CURSOR")" || exit 1
[ -e "$LOG" ] || : > "$LOG"

# "<lines-consumed> <inode> <bytes>". Rotation comes in two shapes and a line
# count alone reads both as "nothing new": a REPLACED file (new inode, possibly
# the same length) and a TRUNCATED one (same inode, shorter). Track both.
# `read` returns 1 at EOF even after setting the variables, which is every time
# for a file with no trailing newline: the values are what matter, not its rc.
N=""; INO_SEEN=""; SIZE_SEEN=""
read -r N INO_SEEN SIZE_SEEN < "$CURSOR" 2>/dev/null || true
case "$N" in ''|*[!0-9]*) N=0 ;; esac
INO_NOW="$(ls -di "$LOG" 2>/dev/null | awk '{print $1}')"
case "$INO_NOW" in ''|*[!0-9]*) INO_NOW=0 ;; esac
log_size() { wc -c < "$LOG" 2>/dev/null | tr -d ' '; }
SIZE_NOW="$(log_size)"
case "$SIZE_NOW" in ''|*[!0-9]*) SIZE_NOW=0 ;; esac
case "$SIZE_SEEN" in ''|*[!0-9]*) SIZE_SEEN="" ;; esac
if [ -n "$INO_SEEN" ] && [ "$INO_SEEN" != "$INO_NOW" ]; then
  N=0                                    # a different file
elif [ -n "$SIZE_SEEN" ] && [ "$SIZE_NOW" -lt "$SIZE_SEEN" ]; then
  N=0                                    # same file, truncated under us
else
  LINES="$(wc -l < "$LOG" 2>/dev/null | tr -d ' ')"
  case "$LINES" in ''|*[!0-9]*) LINES=0 ;; esac
  [ "$N" -gt "$LINES" ] && N=0
fi

write_cursor() { printf '%s %s %s\n' "$1" "$INO_NOW" "$(log_size)" > "$CURSOR"; }
write_cursor "$N"
# `tail -F` never ends on its own: it must be OWNED here, or every re-arm leaves
# one behind holding the log. A pipeline would hide its pid in a subshell.
exec 3< <(tail -n +$((N + 1)) -F "$LOG" 2>/dev/null)
TAILPID=$!
# The heartbeat says "a reader is alive", so it must DIE with the reader — a
# SIGKILL runs no trap, and an orphaned heartbeat would keep the cursor fresh
# forever beside a reader that is gone. It watches the reader's own pid and
# takes the tail with it.
READER=$$
( while :; do
    sleep "${SUTANDO_TAIL_HEARTBEAT_SEC:-20}"
    if ! kill -0 "$READER" 2>/dev/null; then
      kill -TERM "$TAILPID" 2>/dev/null
      exit 0
    fi
    touch "$CURSOR" 2>/dev/null || exit 0
  done ) &
HEARTBEAT=$!
cleanup() {
  kill -TERM "$HEARTBEAT" 2>/dev/null || true
  kill -TERM "$TAILPID" 2>/dev/null || true
  exec 3<&- 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 0' HUP INT TERM

while IFS= read -r line <&3; do
  printf '%s\n' "$line"
  N=$((N + 1))
  write_cursor "$N"
done

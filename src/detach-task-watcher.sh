#!/bin/bash
# Start a session-role task watcher DETACHED from the session that asks for it,
# and print the log its stdout is appended to.
#
# A Monitor-hosted watcher dies with its Monitor: every expiry is a window in
# which nothing announces a delivery, and the session only learns at its next
# turn. Detached, the watcher outlives the Monitor; the session tails the log
# instead, and a tail that expires loses nothing because the log keeps the
# events. Re-arm the tail from the last line the session consumed, never from
# the file's current length, or the gap is skipped in silence.
#
#   bash src/detach-task-watcher.sh --inbox <dir> [--workspace <dir>]
#
# Prints, on stdout:
#   LOG: <path>     the file the watcher's stdout is appended to
#   PID: <n>        the detached watcher
# Exit 0 when a DETACHED watcher writing that log is live afterwards (started
# here, or already running), 3 when the inbox is held by a watcher whose output
# goes elsewhere (a Monitor: nothing is started and no LOG is printed, because
# tailing it would read a file nobody writes), 1 when it could not be started,
# 64 on a usage error.
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
    *) echo "usage: detach-task-watcher.sh --inbox <dir> [--workspace <dir>]" >&2; exit 64 ;;
  esac
done
[ -n "$INBOX" ] || INBOX="${SUTANDO_TASKS_DIR:-}"
if [ -z "$INBOX" ]; then
  echo "detach-task-watcher: refusing to start: no --inbox and no SUTANDO_TASKS_DIR." >&2
  exit 64
fi
INBOX="$(canonical_tasks_dir "$INBOX")"
[ -n "$WORKSPACE" ] || WORKSPACE="${SUTANDO_WORKSPACE_DIR:-$(workspace_dir_for_inbox "$INBOX")}"
PY="$(require_python "$__REPO_ROOT" "detach the task watcher")" || exit 1

LOG="$("$PY" "$__REPO_ROOT/src/util_paths.py" watcher-log "$WORKSPACE" "$INBOX")" || exit 1
mkdir -p "$(dirname "$LOG")" || exit 1
# PHYSICAL, because that is what a reader of /proc or lsof reports back: on macOS
# a workspace under /var is reported as /private/var, and a string compare splits
# one file into two.
LOG="$(cd "$(dirname "$LOG")" && pwd -P)/$(basename "$LOG")"

# Already covered — but by WHICH kind of watcher? A Monitor-hosted one writes to
# its Monitor, not to this log, so reporting the log here would send the caller
# to tail a file nobody writes and lose every task when that Monitor expires.
if [ "$("$PY" "$__REPO_ROOT/src/watcher_identity.py" role-present session \
          --inbox "$INBOX" --ready "$WORKSPACE/state" 2>/dev/null)" = "yes" ]; then
  HOLDER="$("$PY" "$__REPO_ROOT/src/watcher_identity.py" inbox-holders --inbox "$INBOX" 2>/dev/null | awk '$2=="session"{print $1; exit}')"
  SINK="$("$PY" "$__REPO_ROOT/src/watcher_identity.py" output-sink "${HOLDER:-0}" 2>/dev/null | head -1)"
  case "$SINK" in
    "file $LOG")
      echo "LOG: $LOG"
      echo "PID: $HOLDER"
      echo "detach-task-watcher: $INBOX already has a ready detached watcher on this log; nothing started." >&2
      exit 0 ;;
    *)
      # rc 3: covered, but NOT by a watcher writing this log. The caller keeps
      # whatever is hosting it (a Monitor) and must not tail the log.
      echo "PID: ${HOLDER:-unknown}"
      echo "detach-task-watcher: $INBOX is held by pid ${HOLDER:-unknown} whose output is '${SINK:-unreadable}', not $LOG; leaving it alone. Stop that watcher first to detach this inbox." >&2
      exit 3 ;;
  esac
fi

# nohup + setsid-by-python: `setsid` is not on macOS, and a watcher that stays in
# the caller's process group dies with the session it was meant to outlive.
"$PY" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
  bash "$__REPO_ROOT/src/watch-tasks-stream.sh" "$INBOX" --role session --inbox "$INBOX" \
  >> "$LOG" 2>> "${LOG%.log}.stderr.log" < /dev/null &
STARTED=$!
disown "$STARTED" 2>/dev/null || true

# Ready, not merely spawned: the sentinel is what every other reader gates on.
i=0
while [ "$i" -lt "${SUTANDO_DETACH_READY_TIMEOUT:-20}0" ]; do
  [ "$("$PY" "$__REPO_ROOT/src/watcher_identity.py" role-present session \
        --inbox "$INBOX" --ready "$WORKSPACE/state" 2>/dev/null)" = "yes" ] && break
  kill -0 "$STARTED" 2>/dev/null || break
  sleep 0.1
  i=$((i + 1))
done

if [ "$("$PY" "$__REPO_ROOT/src/watcher_identity.py" role-present session \
        --inbox "$INBOX" --ready "$WORKSPACE/state" 2>/dev/null)" != "yes" ]; then
  echo "detach-task-watcher: no ready session watcher on $INBOX after ${SUTANDO_DETACH_READY_TIMEOUT:-20}s; see ${LOG%.log}.stderr.log" >&2
  exit 1
fi
echo "LOG: $LOG"
echo "PID: $("$PY" "$__REPO_ROOT/src/watcher_identity.py" inbox-holders --inbox "$INBOX" 2>/dev/null | awk '$2=="session"{print $1; exit}')"

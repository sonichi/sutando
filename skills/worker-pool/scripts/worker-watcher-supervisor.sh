#!/usr/bin/env bash
# One hosting-mode supervisor per worker inbox: the same standby watcher +
# notifier pair the core runs beside itself, parameterised with THIS worker's
# inbox, tmux session and identity. Idempotent: a running supervisor is left
# alone, and a worker session that is not running gets no supervisor (the
# supervisor's own target gate would end it at once anyway).
#
# Env (the spawner's plan, as launch-worker-session.sh receives it):
#   SUTANDO_INSTANCE_ID   the worker id            (required)
#   SUTANDO_TASKS_DIR     the worker's delivery inbox (required)
#   SUTANDO_TMUX_SESSION  the worker's tmux session (required)
#   SUTANDO_TMUX_SOCKET, SUTANDO_WORKSPACE_DIR, SUTANDO_RESULTS_DIR,
#   SUTANDO_INBOX_KIND, SUTANDO_TASK_EVENT_HANDLER, SUTANDO_PY   forwarded when set
#
# Usage: worker-watcher-supervisor.sh            ensure the supervisor is running
#        worker-watcher-supervisor.sh --print-command   print the tmux argv, run nothing
# Exit 0: running (already, or started now). 3: no worker session to supervise.
set -u
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
: "${SUTANDO_INSTANCE_ID:?worker-watcher-supervisor: SUTANDO_INSTANCE_ID is required}"
: "${SUTANDO_TASKS_DIR:?worker-watcher-supervisor: SUTANDO_TASKS_DIR is required}"
: "${SUTANDO_TMUX_SESSION:?worker-watcher-supervisor: SUTANDO_TMUX_SESSION is required}"
SOCK="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
WORKER_SESSION="$SUTANDO_TMUX_SESSION"
SUP_SESSION="${WORKER_SESSION}-watcher"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
NOTIFIER="$REPO/src/agent/claude/cli/task-notifier.sh"
BEAT="$REPO/skills/worker-pool/scripts/pool_beat.py"
PY="${SUTANDO_PY:-python3}"

# The tmux server's env is what a new session inherits, never this shell's, so
# every value the supervisor, notifier and standby watcher read is set with -e.
ENV_ARGS=(
  -e "SUTANDO_TMUX_SOCKET=$SOCK"
  -e "SUTANDO_TMUX_SESSION=$WORKER_SESSION"
  -e "SUTANDO_TMUX_WINDOW=0"
  -e "SUTANDO_NOTIFIER_SCRIPT=$NOTIFIER"
  -e "SUTANDO_NOTIFIER_PY=$PY"
  -e "SUTANDO_TASKS_DIR=$SUTANDO_TASKS_DIR"
  -e "SUTANDO_INSTANCE_ID=$SUTANDO_INSTANCE_ID"
  -e "SUTANDO_WATCHER_BEAT=$BEAT"
)
for v in SUTANDO_WORKSPACE_DIR SUTANDO_RESULTS_DIR SUTANDO_INBOX_KIND \
         SUTANDO_TASK_EVENT_HANDLER SUTANDO_NOTIFIER_GRACE_PERIOD SUTANDO_NOTIFIER_ROLE_POLL; do
  if [ -n "${!v:-}" ]; then ENV_ARGS+=(-e "$v=${!v}"); fi
done
CMD=(tmux -S "$SOCK" new-session -d -s "$SUP_SESSION" "${ENV_ARGS[@]}" bash "$SUPERVISOR")

if [ "${1:-}" = "--print-command" ]; then
  printf '%s\n' "${CMD[@]}"
  exit 0
fi
if tmux -S "$SOCK" has-session -t "=$SUP_SESSION" 2>/dev/null; then
  echo "worker-watcher-supervisor: $SUP_SESSION already running"
  exit 0
fi
if ! tmux -S "$SOCK" has-session -t "=$WORKER_SESSION" 2>/dev/null; then
  echo "worker-watcher-supervisor: worker session $WORKER_SESSION is not running; nothing to supervise" >&2
  exit 3
fi
"${CMD[@]}" || { echo "worker-watcher-supervisor: tmux could not start $SUP_SESSION" >&2; exit 1; }
echo "worker-watcher-supervisor: started $SUP_SESSION for $SUTANDO_TASKS_DIR"

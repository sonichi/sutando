#!/bin/bash
# Convert watcher events into queued prompts for the interactive Codex core.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
  TASKS_DIR="${SUTANDO_TASKS_DIR/#\~/$HOME}"
else
  TASKS_DIR="$(bash "$REPO/scripts/sutando-config.sh" workspace)/tasks"
fi
RESULTS_DIR="${SUTANDO_RESULTS_DIR:-$(dirname "$TASKS_DIR")/results}"

submit_task() {
  local filename="$1" prompt
  case "$filename" in
    ""|*/*|*..*) return 0 ;;
  esac
  prompt="Sutando task ready: $filename. Read $TASKS_DIR/$filename, follow AGENTS.md, complete the task, and write the result to $RESULTS_DIR/$filename."
  if ! tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null; then
    exit 0
  fi
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" -l -- "$prompt"
  # Give the interactive TUI one render tick to consume the literal paste
  # before submitting it. Without this delay, a newly-idle live Codex pane can
  # receive C-m first and leave the full task prompt staged but not dispatched.
  sleep 0.15
  # Codex's TUI treats an explicit carriage return as submit. tmux's symbolic
  # `Enter` can be rendered as an input newline without dispatching the turn on
  # current Codex builds; C-m is the reliable terminal submit sequence.
  tmux -S "$TMUX_SOCKET" send-keys -t "$SESSION:0" C-m
}

if [ "${1:-}" = "--event" ]; then
  [ -n "${2:-}" ] || { echo "task-notifier: --event requires a filename" >&2; exit 2; }
  submit_task "$2"
  exit 0
fi

bash "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" | while IFS= read -r event; do
  case "$event" in
    "TASK_FILE: "*) submit_task "${event#TASK_FILE: }" ;;
  esac
done

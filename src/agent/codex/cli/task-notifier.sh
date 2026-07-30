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
POLL_INTERVAL="${SUTANDO_NOTIFIER_POLL_INTERVAL:-0.5}"
COMPLETION_TIMEOUT="${SUTANDO_NOTIFIER_COMPLETION_TIMEOUT:-3600}"

has_result() {
  local filename="$1" stem archive_dir
  [ -f "$RESULTS_DIR/$filename" ] && return 0
  stem="${filename%.txt}"
  # Local bridges archive as archive/YYYY-MM/<task>.txt. The remote gateway
  # archives as archive/<task>-<epoch>.txt. Startup retention uses sibling
  # archive-YYYY-MM-DD/<task>.txt directories. All are completed deliveries.
  if [ -d "$RESULTS_DIR/archive" ] && find "$RESULTS_DIR/archive" \
      -mindepth 1 -maxdepth 2 -type f \
      \( -name "$filename" -o -name "$stem-[0-9]*.txt" \) -print -quit 2>/dev/null \
      | grep -q .; then
    return 0
  fi
  for archive_dir in "$RESULTS_DIR"/archive-*; do
    [ -d "$archive_dir" ] || continue
    if find "$archive_dir" -mindepth 1 -maxdepth 1 -type f \
        \( -name "$filename" -o -name "$stem-[0-9]*.txt" \) -print -quit 2>/dev/null \
        | grep -q .; then
      return 0
    fi
  done
  return 1
}

submit_task() {
  local filename="$1" wait_for_result="${2:-0}" prompt started
  case "$filename" in
    ""|*/*|*..*) return 0 ;;
  esac
  # The stream watcher deliberately sweeps pre-existing task files after a
  # restart. Completed tasks remain in tasks/ for dashboard history, so do not
  # replay any task whose bridge result already exists.
  has_result "$filename" && return 0
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

  # Codex's interactive input is not a durable multi-message queue: sending a
  # second prompt while the first turn is starting can replace or interleave
  # input. The managed watcher therefore releases one task at a time and uses
  # the bridge result as the completion acknowledgement. `--event` remains a
  # fire-and-forget diagnostic hook.
  if [ "$wait_for_result" = "1" ]; then
    started="$(date +%s)"
    while ! has_result "$filename"; do
      session_exists=0
      tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null && session_exists=1
      [ "$session_exists" = "1" ] || return 0
      if [ $(( $(date +%s) - started )) -ge "$COMPLETION_TIMEOUT" ]; then
        echo "task-notifier: timed out waiting for result: $filename" >&2
        return 0
      fi
      sleep "$POLL_INTERVAL"
    done
  fi
}

if [ "${1:-}" = "--event" ]; then
  [ -n "${2:-}" ] || { echo "task-notifier: --event requires a filename" >&2; exit 2; }
  submit_task "$2"
  exit 0
fi

bash "$REPO/src/watch-tasks-stream.sh" "$TASKS_DIR" | while IFS= read -r event; do
  case "$event" in
    "TASK_FILE: "*) submit_task "${event#TASK_FILE: }" 1 ;;
  esac
done

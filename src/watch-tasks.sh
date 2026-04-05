#!/bin/bash
# Watch for new task files in tasks/ directory — persistent loop.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Loops forever, printing each batch of tasks as they arrive.
# The caller reads output and processes tasks without restarting.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

# Ensure required directories exist
mkdir -p "$TASKS_DIR"
mkdir -p "$(dirname "$0")/../results/calls"

emit_tasks() {
  echo "TASK_DETECTED: Process ALL .txt files in tasks/."
  for f in "$TASKS_DIR"/*.txt; do echo "--- $(basename "$f") ---"; cat "$f"; done
}

while true; do
  # Check for existing files BEFORE waiting (catches files written during restarts)
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    emit_tasks
  fi
  # Wait for next filesystem event
  CHANGED=$(fswatch -1 -l 1 "$TASKS_DIR" 2>/dev/null)
  echo "fswatch triggered: $CHANGED"
  # Check again after event
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    emit_tasks
  fi
  # Loop back — keep watching
done

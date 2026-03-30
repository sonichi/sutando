#!/bin/bash
# Watch for new task files in tasks/ directory.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Uses fswatch -1 to detect one new file, then exits.
# The caller processes tasks and restarts.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

# Wait for a new .txt file — loop until one actually appears
while true; do
  # Check for existing files BEFORE waiting (catches files written during restarts)
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    echo "TASK_DETECTED: Process ALL .txt files in tasks/."
    ls "$TASKS_DIR"/*.txt
    break
  fi
  # Wait for next filesystem event
  fswatch -1 -l 1 "$TASKS_DIR" >/dev/null 2>&1
  # Check again after event
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    echo "TASK_DETECTED: Process ALL .txt files in tasks/."
    ls "$TASKS_DIR"/*.txt
    break
  fi
  # False trigger — keep watching
done

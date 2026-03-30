#!/bin/bash
# Watch for new task files in tasks/ directory.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Uses fswatch -1 to detect one new file, then exits.
# The caller processes tasks and restarts.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

# Exit if another fswatch process is already watching tasks/
# Use pgrep -x to match only the fswatch binary, not this script
if pgrep -x fswatch >/dev/null 2>&1; then
  exit 0
fi

# Wait for a new .txt file — loop until one actually appears
while true; do
  fswatch -1 -l 1 "$TASKS_DIR" >/dev/null 2>&1
  # Only exit if .txt files actually exist
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    echo "TASK_DETECTED: Process ALL .txt files in tasks/."
    ls "$TASKS_DIR"/*.txt
    break
  fi
  # False trigger — keep watching
done

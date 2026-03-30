#!/bin/bash
# Watch for new task files in tasks/ directory.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Uses fswatch -1 to detect one new file, then exits.
# The caller processes tasks and restarts.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

# Exit if another watcher is already running (don't kill it)
if pgrep -f "fswatch.*tasks" >/dev/null 2>&1; then
  exit 0
fi

# Wait for a new file event
fswatch -1 --event Created --event Updated --include '\.txt$' --exclude '.*' "$TASKS_DIR" >/dev/null 2>&1

# Report what's there
echo "TASK_DETECTED: Process ALL .txt files in tasks/."
ls "$TASKS_DIR"/*.txt 2>/dev/null || true

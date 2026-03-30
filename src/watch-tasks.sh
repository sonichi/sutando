#!/bin/bash
# Watch for new task files in tasks/ directory.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Uses fswatch -1 (one-shot) to detect new files, then exits with a message.
# The caller restarts this after processing tasks.
# Dedup: kills any existing fswatch watcher before starting.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

# Kill any existing fswatch watchers to prevent duplicates
# Use grep -v $$ to avoid killing ourselves
for pid in $(pgrep -f "fswatch.*tasks" 2>/dev/null); do
  if [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ]; then
    kill "$pid" 2>/dev/null
  fi
done

# Watch for new files (blocks until a change is detected)
fswatch -1 --event Created --event Updated "$TASKS_DIR" >/dev/null 2>&1

# Output what was found
echo "TASK_DETECTED: Process ALL .txt files in tasks/ before restarting the watcher."
ls "$TASKS_DIR"/*.txt 2>/dev/null || true

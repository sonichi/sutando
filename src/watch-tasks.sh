#!/bin/bash
# Watch for new task files in tasks/ directory.
# Usage: bash src/watch-tasks.sh (run_in_background: true)
#
# Runs a persistent polling loop (2s interval) instead of fswatch.
# Never exits — survives fswatch crashes, catches tasks immediately.
# The caller reads the output file for TASK_DETECTED lines.

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"

echo "[watcher] Started persistent task watcher on $TASKS_DIR"

while true; do
  # Check for .txt files
  if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
    echo "TASK_DETECTED: Process ALL .txt files in tasks/."
    for f in "$TASKS_DIR"/*.txt; do echo "--- $(basename "$f") ---"; cat "$f"; done
    echo "TASK_DETECTED_END"
    # Wait for files to be consumed before checking again
    while ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; do
      sleep 2
    done
  fi
  sleep 2
done

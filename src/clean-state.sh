#!/bin/bash
# Clean up stale tasks, results, and status files

REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "Cleaning Sutando state..."

# Remove old task files
rm -f "$REPO/tasks/"*.txt 2>/dev/null

# Remove old result files (but keep voice-conversation.json)
find "$REPO/results" -name "task-*.txt" -mmin +5 -delete 2>/dev/null
find "$REPO/results" -name "status-*.txt" -delete 2>/dev/null
find "$REPO/results" -name "narration-*.txt" -delete 2>/dev/null
find "$REPO/results" -name "proactive-*.txt" -delete 2>/dev/null
rm -f "$REPO/results/voice-conversation.json" 2>/dev/null

echo "Done. Tasks and stale results cleared."

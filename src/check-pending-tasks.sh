#!/bin/bash
# Hook script: checks for unprocessed task files in tasks/.
# Used as a Stop hook to prevent Claude from finishing a response
# while tasks remain unprocessed.
# Output: JSON with additionalContext if tasks found.

TASKS_DIR="$(cd "$(dirname "$0")/.." && pwd)/tasks"

# Check for .txt task files
if ls "$TASKS_DIR"/*.txt >/dev/null 2>&1; then
  CONTENT="UNPROCESSED TASKS FOUND — you MUST process these before stopping:\n"
  for f in "$TASKS_DIR"/*.txt; do
    CONTENT+="\n--- $(basename "$f") ---\n"
    CONTENT+="$(cat "$f")\n"
  done
  # Block stopping and inject task content
  printf '{"decision":"block","reason":"Unprocessed tasks in tasks/ directory","additionalContext":"%s"}' "$(echo -e "$CONTENT" | sed 's/"/\\"/g' | tr '\n' ' ')"
else
  # No tasks — allow stop
  echo '{}'
fi

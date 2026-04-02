#!/bin/bash
# Stop hook: blocks Claude from finishing when unprocessed tasks exist.
# Skips tasks that already have a corresponding result file.

TASKS_DIR="$(cd "$(dirname "$0")/.." && pwd)/tasks"
RESULTS_DIR="$(cd "$(dirname "$0")/.." && pwd)/results"

UNPROCESSED=""
shopt -s nullglob 2>/dev/null
for f in "$TASKS_DIR"/*.txt; do
  BASENAME=$(basename "$f")
  # Skip if result already exists
  [ -f "$RESULTS_DIR/$BASENAME" ] && continue
  UNPROCESSED+="--- $BASENAME ---\n$(cat "$f")\n\n"
done

if [ -n "$UNPROCESSED" ]; then
  printf '{"decision":"block","reason":"Unprocessed tasks in tasks/","additionalContext":"UNPROCESSED TASKS — process these NOW:\n%s"}' "$(echo -e "$UNPROCESSED" | sed 's/"/\\"/g' | tr '\n' ' ')"
else
  echo '{}'
fi

#!/bin/bash
# One-shot cleanup: move stranded .claimed-core-*.txt files from tasks/ to
# tasks/archive/YYYY-MM/ (#933). Run once after deploying the bridge fix.
set -e
WORKSPACE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
TASKS_DIR="$WORKSPACE/tasks"
YM=$(date +%Y-%m)
DEST="$TASKS_DIR/archive/$YM"

count=0
while IFS= read -r -d '' f; do
  mkdir -p "$DEST"
  mv "$f" "$DEST/"
  echo "  swept: $(basename "$f")"
  count=$((count + 1))
done < <(find "$TASKS_DIR" -maxdepth 1 -name "*.claimed-core-*.txt" -print0 2>/dev/null)

echo "Swept $count stranded claim file(s) → $DEST/"

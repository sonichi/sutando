#!/usr/bin/env bash
# One-shot cleanup: archive .claimed-core-*.txt files that are older than MIN_AGE_HOURS
# and were stranded in tasks/ before PR #1124 fixed bridge archiving.
# Safe to run multiple times — already-archived files are skipped.
set -euo pipefail

WORKSPACE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
TASKS_DIR="$WORKSPACE/tasks"
MIN_AGE_HOURS="${MIN_AGE_HOURS:-24}"

if [ ! -d "$TASKS_DIR" ]; then
  echo "tasks/ not found at $TASKS_DIR — nothing to do"
  exit 0
fi

ARCHIVE_BASE="$WORKSPACE/archive/tasks"
now=$(date +%s)
min_age_secs=$(( MIN_AGE_HOURS * 3600 ))
count=0

while IFS= read -r -d '' f; do
  # Extract YYYY-MM from file mtime for archive subdir
  mtime=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null)
  age=$(( now - mtime ))
  if [ "$age" -lt "$min_age_secs" ]; then
    continue
  fi
  ym=$(date -r "$mtime" +%Y-%m 2>/dev/null || date -d "@$mtime" +%Y-%m 2>/dev/null)
  dest_dir="$ARCHIVE_BASE/$ym"
  mkdir -p "$dest_dir"
  dest="$dest_dir/$(basename "$f")"
  if [ -f "$dest" ]; then
    echo "  skip (already archived): $(basename "$f")"
    rm -f "$f"
    continue
  fi
  mv "$f" "$dest"
  echo "  archived: $(basename "$f") → archive/tasks/$ym/"
  count=$(( count + 1 ))
done < <(find "$TASKS_DIR" -maxdepth 1 -name '*.claimed-core-*.txt' -print0)

echo "Done — $count file(s) swept."

#!/bin/bash
# Channel-aware task watcher (skill) — a thin WRAPPER around the core
# src/watch-tasks-stream.sh. It runs the core watcher unchanged and appends the
# originating channel to each `TASK_FILE: <name>` line it emits, so the consumer
# can never mismatch a reply to the wrong channel (see the 2026-06-03 leak;
# memory feedback_sensitive_content_dm_only).
#
# Output per event:
#   TASK_FILE: <basename>  [ch: <channel_name> <channel_id>]
# (the suffix is omitted if the file has no channel_* fields or is already gone)
#
# Core detection logic stays the single source of truth in src/ — this skill
# does NOT edit it. If this skill is removed, the core watcher still works.
set -u

# Locate the repo (for the core watcher) and the tasks dir (to read channels).
REPO="${SUTANDO_REPO_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)}"
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  TASKS_DIR="${SUTANDO_WORKSPACE/#\~/$HOME}/tasks"
else
  TASKS_DIR="$HOME/.sutando/workspace/tasks"
fi
CORE="$REPO/src/watch-tasks-stream.sh"

# Look up the channel for a task basename. channel_id is numeric (always safe);
# channel_name is newline-stripped and truncated for display.
_chan_of() {
  local f="$TASKS_DIR/$1" cid cname
  [ -f "$f" ] || return 0
  cid="$(grep -m1 '^channel_id:' "$f" 2>/dev/null | cut -d: -f2- | tr -d ' \r')"
  cname="$(grep -m1 '^channel_name:' "$f" 2>/dev/null | cut -d: -f2- | tr -d '\r' | sed 's/^ //' | cut -c1-40)"
  [ -n "$cid$cname" ] && printf '  [ch: %s %s]' "$cname" "$cid"
}

# Run the core watcher and augment its output. When the consumer pipe dies, the
# core watcher takes SIGPIPE on its next printf and exits (it has `|| exit 0`),
# so this wrapper's read loop ends too.
bash "$CORE" | while IFS= read -r line; do
  case "$line" in
    "TASK_FILE: "*)
      base="${line#TASK_FILE: }"
      printf 'TASK_FILE: %s%s\n' "$base" "$(_chan_of "$base")"
      ;;
    *)
      printf '%s\n' "$line"
      ;;
  esac
done

#!/bin/bash
# Streaming task watcher — companion to watch-tasks.sh.
#
# Runs fswatch indefinitely and emits ONE line per new task file appearance.
# Designed to be invoked via Claude Code's `Monitor` tool, which streams
# stdout lines as per-event notifications without process-restart cycles.
#
# Compared to watch-tasks.sh (one-shot, exits on first event so the caller
# can be notified via process-exit): this script never exits during normal
# operation. Migration is gated by which invocation pattern the agent uses
# (Bash run_in_background vs Monitor), not by removing the old script.
#
# Output format per event:
#   TASK_FILE: <basename>
# Plus an INITIAL_SCAN block at startup for any pre-existing files:
#   TASK_FILE: <basename>  (one per line)
#
# The agent reads the named files via the Read tool when notifications
# arrive — no need to inline file contents in stdout (Monitor's 200ms
# batching window would group multi-line content awkwardly).

set -u

TASKS_DIR="${1:-$(dirname "$0")/../tasks}"
mkdir -p "$TASKS_DIR"

# Initial sweep — surface any pre-existing tasks that arrived during a
# restart gap.
shopt -s nullglob
for f in "$TASKS_DIR"/*.txt; do
  echo "TASK_FILE: $(basename "$f")"
done
shopt -u nullglob

# Stream subsequent events. -l 0.5 = 500ms latency batch (fswatch coalesces
# burst events). --event Created --event Renamed catches new file
# appearance whether it lands as a fresh write or a rename-into-place.
#
# Existence check after match: fswatch fires Renamed events on BOTH ends of
# a rename (source path AND destination path). For tasks/ that means
# `mv tasks/X.txt tasks/archive/Y/` triggers a Renamed event for the source
# `tasks/X.txt` AFTER the file has moved out — emitting a spurious
# `TASK_FILE: X.txt` for a file no longer in the watched dir. The
# `[ -f "$path" ]` test filters out those rename-OUT-of-watched-dir events
# while still letting rename-INTO-place events through (file does exist at
# the watched path). Caught 2026-05-03 during the live Monitor-mode rollout.
fswatch \
  -l 0.5 \
  --event Created \
  --event Renamed \
  "$TASKS_DIR" 2>/dev/null \
| while IFS= read -r path; do
  case "$path" in
    *.txt)
      if [ -f "$path" ]; then
        echo "TASK_FILE: $(basename "$path")"
      fi
      ;;
  esac
done

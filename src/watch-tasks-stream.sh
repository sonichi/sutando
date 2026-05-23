#!/bin/bash
# Streaming task watcher — the canonical task-detection path.
#
# Runs fswatch indefinitely and emits ONE line per new task file appearance.
# Designed to be invoked via Claude Code's `Monitor` tool, which streams
# stdout lines as per-event notifications without process-restart cycles.
#
# Replaces the one-shot `watch-tasks.sh` (retired 2026-05-14) — that one
# exited on first event so the caller had to restart it; this one stays
# alive for the lifetime of the CLI session.
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

# Resolve TASKS_DIR. Priority: explicit positional arg → $SUTANDO_WORKSPACE/tasks
# → canonical default `~/.sutando/workspace/tasks` (matching
# `workspace_default.resolve_workspace()` — the shared contract every bridge
# already follows). The bridges (discord-bridge.py, telegram-bridge.py,
# dm-result.py — see PRs #708/#720/#722/#723) write to that default when env
# is unset; if this watcher fell back to `<repo>/tasks/` instead, the bridges
# would write to one dir and the watcher would poll another, so owner DMs land
# silently. Diagnosed 2026-05-15 (~3 dropped DMs over 17 min) and again
# 2026-05-16 (~45 min silent gap when the Monitor was started without
# SUTANDO_WORKSPACE exported into its env) — second incident motivated
# replacing the legacy `<repo>/tasks` fallback with the workspace default so
# the divergence can't happen even when callers forget to export.
if [ -n "${1:-}" ]; then
  TASKS_DIR="$1"
elif [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  TASKS_DIR="$SUTANDO_WORKSPACE/tasks"
else
  TASKS_DIR="$HOME/.sutando/workspace/tasks"
fi
mkdir -p "$TASKS_DIR"
# Canonicalize watched dir for the parent-dir filter below. fswatch always
# emits PHYSICAL paths (e.g. /private/tmp/... not /tmp/...), so we resolve
# symlinks with `pwd -P` to match. Without -P, on macOS the comparison
# `dirname "$path"` == `$TASKS_DIR_ABS` fails when /tmp is symlinked to
# /private/tmp — which is the default.
TASKS_DIR_ABS="$(cd "$TASKS_DIR" && pwd -P)"

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
# TWO filters before emit:
#
# 1. Parent-dir match: the macOS FSEvents monitor (fswatch's default) is
#    recursive even without `-r`, so a rename from `tasks/X.txt` to
#    `tasks/archive/.../X.txt` fires events for BOTH the source AND the
#    destination — and the destination path is in a subdir we don't care
#    about. We only want events for files that landed AS A DIRECT CHILD
#    of $TASKS_DIR. `dirname "$path"` against the absolute watched dir
#    catches this. Caught 2026-05-03 #2: archives in tasks/archive/2026-05/
#    were re-firing TASK_FILE: <name> with a different path but the same
#    basename, making the agent re-process every just-archived task.
#
# 2. Existence check: fswatch fires Renamed events on BOTH ends of a
#    rename — including the source path AFTER the file has moved out.
#    `[ -f "$path" ]` filters those rename-OUT-of-watched-dir events.
#    Caught 2026-05-03 #1 (PR #572).
fswatch \
  -l 0.5 \
  --event Created \
  --event Renamed \
  "$TASKS_DIR" 2>/dev/null \
| while IFS= read -r path; do
  case "$path" in
    *.txt)
      parent="$(dirname "$path")"
      if [ "$parent" = "$TASKS_DIR_ABS" ] && [ -f "$path" ]; then
        echo "TASK_FILE: $(basename "$path")"
      fi
      ;;
  esac
done

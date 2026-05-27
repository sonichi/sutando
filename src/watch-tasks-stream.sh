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

# PID file for the Stop-hook cleanup path (see .claude/settings.json Stop
# hook). When a Claude Code session ends, the Stop hook reads this file and
# kills the watcher PID it points at, so the fswatch process doesn't outlive
# the session and turn into an orphan. The trap below removes the file on a
# clean exit; the Stop hook removes it after the kill on dirty exits.
#
# Same workspace resolution as TASKS_DIR (above): explicit env override,
# else canonical default. Living under state/ matches the workspace contract
# in CLAUDE.md (loose status/state files belong there).
if [ -n "${SUTANDO_WORKSPACE:-}" ]; then
  STATE_DIR="${SUTANDO_WORKSPACE/#\~/$HOME}/state"
else
  STATE_DIR="$HOME/.sutando/workspace/state"
fi
mkdir -p "$STATE_DIR"
PID_FILE="$STATE_DIR/watch-tasks-stream.pid"
# PID file tracks fswatch itself (not this bash wrapper) so Stop hook /
# startup reaper kill fswatch directly. When fswatch exits, the while-read
# loop sees EOF on the FIFO and exits cleanly without requiring a second kill.
# (The old design stored $$, leaving fswatch as an orphan after the wrapper
# died — Mode B from issue #1088.)
_FIFO="$(mktemp -u "${TMPDIR:-/tmp}/watch-tasks-XXXXXX")"
mkfifo "$_FIFO"

fswatch \
  -l 0.5 \
  --event Created \
  --event Renamed \
  "$TASKS_DIR" 2>/dev/null \
> "$_FIFO" &
FSWATCH_PID=$!

echo "$FSWATCH_PID" > "$PID_FILE"
# Cleanup: kill fswatch, remove PID file and FIFO. Fires on SIGINT/SIGTERM
# and on normal exit. SIGKILL skips the trap — Stop hook / startup reaper
# read the PID file and kill fswatch on the next session start.
trap 'kill "$FSWATCH_PID" 2>/dev/null; rm -f "$PID_FILE" "$_FIFO"' EXIT INT TERM

# Initial sweep — surface any pre-existing tasks that arrived during a
# restart gap.
shopt -s nullglob
for f in "$TASKS_DIR"/*.txt; do
  echo "TASK_FILE: $(basename "$f")"
done
shopt -u nullglob

# Read fswatch events from the FIFO. Two filters applied before emit:
#
# 1. Parent-dir match: macOS FSEvents is recursive even without -r; a
#    rename from tasks/X.txt → tasks/archive/.../X.txt fires events for
#    BOTH paths. We only want direct children of $TASKS_DIR. (PR #572 #2)
#
# 2. Existence check: fswatch fires Renamed on both source AND dest; the
#    source event arrives AFTER the file has moved out, so -f skips it.
#    (PR #572 #1)
#
# Mode A fix (issue #1088): printf || exit 0 dies on EPIPE immediately
# instead of buffering ~64KB (~100 events) before detecting the dead reader.
# The old `echo` silently queued events into the kernel pipe buffer; real
# DM traffic (few events/day) meant the buffer took days to fill, so events
# arrived on disk but the agent never received TASK_FILE notifications.
while IFS= read -r path; do
  case "$path" in
    *.txt)
      parent="$(dirname "$path")"
      if [ "$parent" = "$TASKS_DIR_ABS" ] && [ -f "$path" ]; then
        printf 'TASK_FILE: %s\n' "$(basename "$path")" || exit 0
      fi
      ;;
  esac
done < "$_FIFO"

wait "$FSWATCH_PID" 2>/dev/null

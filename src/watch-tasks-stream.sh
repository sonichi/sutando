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
echo "$$" > "$PID_FILE"
# Cleanup on any exit path (SIGINT, SIGTERM, normal exit) so the file
# doesn't outlive the process on a clean shutdown. Dirty exits (SIGKILL,
# panic) skip the trap — the Stop hook + startup reaper cover those.
trap 'rm -f "$PID_FILE"' EXIT

# tmux socket for the wakeup signal. Sutando.app creates the CLI session via
# this socket. If the socket doesn't exist (different setup), wakeup is a
# silent no-op thanks to 2>/dev/null || true.
TMUX_SOCK="${SUTANDO_TMUX_SOCK:-/tmp/sutando-tmux.sock}"
TMUX_SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"

# Wake helper, kept but NOT called on the task paths below. Under the only
# launch path that exists — Claude Code's `Monitor` tool (CLAUDE.md, the
# schedule-crons / proactive-loop / startup skills, and the menu-app restart) —
# Monitor re-invokes the session on each stdout line, which wakes an IDLE
# session on its own (controlled test 2026-06-13: synthetic task processed in
# ~30s with no poke — see reference_monitor_notification_wakes_idle_session).
# So calling this per task only duplicated the wake and spammed the CLI input
# line on a restart sweep (Chi saw 7-in-a-row, 2026-06-13). The calls were
# removed in #1679. The helper stays for a future setup that runs this watcher
# WITHOUT a Monitor consuming stdout (a bare background process in a tmux
# session) — wire it back into the loops below if you build that path.
# shellcheck disable=SC2317  # defined-but-unreferenced is intentional
_tmux_wake() {
  # Poke the idle CLI session so it processes the new task without waiting
  # for the next 5-min proactive-loop cron tick (sutando-skills#27 / #1289).
  tmux -S "$TMUX_SOCK" send-keys -t "$TMUX_SESSION" '[watcher-ping]' Enter 2>/dev/null || true
}

# Initial sweep — surface any pre-existing tasks that arrived during a
# restart gap.
shopt -s nullglob
for f in "$TASKS_DIR"/*.txt; do
  printf 'TASK_FILE: %s\n' "$(basename "$f")" || exit 0
done
shopt -u nullglob

# Clean up fswatch on exit (Mode B fix — #1088). Without this, when the
# parent shell exits the watcher reparents to launchd (PPID=1) and runs
# indefinitely with no consumer, silently dropping every event.
cleanup() { kill 0 2>/dev/null; }
trap cleanup EXIT HUP INT TERM

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
#
# Mode A fix (#1088): `|| exit 0` on printf — if the consumer pipe is
# dead, the first failed write exits immediately instead of silently
# buffering ~100 events into the kernel pipe buffer.
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
        printf 'TASK_FILE: %s\n' "$(basename "$path")" || exit 0
      fi
      ;;
  esac
done

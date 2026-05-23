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

# Orphan-watcher self-defense (restart-safety #5).
#
# When the Monitor wrapper / shell redirect at the read end of our stdout
# goes away (Claude session compaction, /pull-and-restart, ⌘Q, etc.) but
# this script's subprocess survives, every subsequent `echo TASK_FILE:`
# silently buffers in the macOS pipe (~64KB) until SIGPIPE — but
# DM-frequency events (a few per day) never fill that buffer, so an
# orphaned watcher can swallow tasks for days before self-dying. Repro
# confirmed 2026-05-23 on Mac Studio + MBP: orphan window is
# env-dependent — Studio (bash 5+) survived 100+ dead-pipe events; MBP
# (bash 3.2) died after ~5. Neither is fast enough.
#
# Two layers of defense below + matching `|| exit 0` on every `echo`:
#
# (1) Every `echo "TASK_FILE: ..."` is appended with `|| exit 0` so any
#     write failure (EPIPE / EBADF / closed FD) exits cleanly instead of
#     looping forever.
#
# (2) Heartbeat: a 30s background loop writes a comment line `# heartbeat
#     <ts>` to stdout. Consumers (Monitor / agent loop) ignore lines
#     starting with `#`. If the heartbeat write fails, the heartbeat
#     subprocess kills the watcher parent. This bounds the orphan window
#     to ≤30s regardless of event volume — independent of pipe buffer
#     size + env-dependent SIGPIPE handling.
PARENT_PID=$$
(
  # Trap SIGPIPE explicitly: bash's default PIPE handler terminates the
  # process before any `||` clause can run, so without this trap the
  # heartbeat dies silently when stdout closes and the parent watcher
  # keeps running. With the trap, EPIPE → trap fires → kill parent.
  trap 'kill '"$PARENT_PID"' 2>/dev/null; exit 0' PIPE
  while sleep 30; do
    echo "# heartbeat $(date -u +%s)" || { kill "$PARENT_PID" 2>/dev/null; exit 0; }
  done
) &
HEARTBEAT_PID=$!
# Main script: same SIGPIPE handling — any `echo TASK_FILE` to a closed
# stdout (e.g. agent loop / Monitor wrapper exits) triggers clean exit
# instead of either zombie-running or being SIGPIPE-killed mid-emit
# without firing the EXIT trap that cleans up the heartbeat.
trap 'kill '"$HEARTBEAT_PID"' 2>/dev/null; exit 0' PIPE
trap "kill $HEARTBEAT_PID 2>/dev/null; exit" INT TERM EXIT

# Initial sweep — surface any pre-existing tasks that arrived during a
# restart gap.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
shopt -s nullglob
for f in "$TASKS_DIR"/*.txt; do
  # Restart-safety #4: bump the `attempts:` counter BEFORE emit.
  # Initial-sweep emissions either reflect a fresh-on-disk task or a
  # leftover from a prior crash; bumping conveys retry-count
  # semantics to the agent (attempts > 1 → agent treats as retry).
  python3 "$SCRIPT_DIR/task_bump_attempts.py" "$f" 2>/dev/null || true
  echo "TASK_FILE: $(basename "$f")" || exit 0
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
        # Restart-safety #4: bump `attempts:` BEFORE emit. For
        # fresh-file events this sets attempts=1; fswatch dedupe
        # prevents same-session double-bumps from burst events.
        python3 "$SCRIPT_DIR/task_bump_attempts.py" "$path" 2>/dev/null || true
        echo "TASK_FILE: $(basename "$path")" || exit 0
      fi
      ;;
  esac
done

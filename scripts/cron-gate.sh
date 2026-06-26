#!/usr/bin/env bash
# Cron prompt gate — defer when owner work is queued.
#
# Wrap each non-loop cron's prompt body so it short-circuits if any owner task
# is still queued in <workspace>/tasks/. This keeps cron noise from competing
# with owner DMs/voice/etc. when /proactive-loop hasn't processed the queue yet.
#
# Usage:
#   bash scripts/cron-gate.sh <reason> <command...>
#
#   - <reason>: short label printed in the deferral message (e.g. "sync-workspace")
#   - <command...>: the actual command to run if the queue is empty
#
# Example crons.example.json entry:
#   "prompt": "Run: bash scripts/cron-gate.sh sync-workspace bash scripts/sync-workspace.sh"
#
# Exit codes:
#   0 — either deferred (queue non-empty) OR the wrapped command exited 0
#   Otherwise — propagates the wrapped command's exit code (via exec)
#
# Loop exemption: /proactive-loop MUST NOT be gated — it's the owner-task handler
# itself. Skipping it on a non-empty queue would deadlock the queue.
set -eu

if [ $# -lt 2 ]; then
  echo "usage: $0 <reason> <command...>" >&2
  exit 2
fi

# Workspace resolution via the canonical M0 helper (PR #1395).
SCRIPT_PARENT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WORKSPACE="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" workspace)"

reason="$1"
shift

# Defer if any task-*.txt is queued (top-level only; archive/processed subdirs
# don't count). find is safer than ls + glob for empty-dir / non-existent-dir.
if [ -d "$WORKSPACE/tasks" ] && [ -n "$(find "$WORKSPACE/tasks" -maxdepth 1 -name 'task-*.txt' -print -quit 2>/dev/null)" ]; then
  echo "cron-gate: owner tasks queued — deferring $reason (will retry next fire)"
  exit 0
fi

exec "$@"

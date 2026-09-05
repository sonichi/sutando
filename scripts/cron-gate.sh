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

# Defer if any OWNER task-*.txt is queued (top-level only; archive/processed
# subdirs don't count). find is safer than ls + glob for empty-dir /
# non-existent-dir.
#
# Exclude task-cron-*.txt: those are emitted by src/cron-runner.py (the launchd
# cron owner) as its delivery vehicle for `launchd: true` entries, carrying
# user_id: cron-runner / priority: low — machine work, never the human-owner
# DMs/voice this gate exists to yield to. Without the exclusion a cron-gate-
# wrapped entry that has been migrated to launchd defers on its own emitted
# file every fire, silently and permanently. This is the gate-side (root) half
# of the fix; the eligibility-side half (don't migrate gated entries) landed in
# reconcile_launchd.py.
#
# task-workstream-grouping-* / task-project-grouping-* are emitted only while
# the core is idle and declare access_tier: owner, so the tier filter misses them.
# Tier filter: a task that EXPLICITLY declares a non-owner access_tier (team /
# guest / collaborator, or a legacy spelling) is peer or public traffic, not the human-owner DMs/voice this
# gate exists to yield to. Deferring on it starves the cron for as long as peers
# keep talking -- observed 2026-08-03: both pending-questions and sync-workspace
# deferred on two #bot2bot notices, with 6 team-tier tasks arriving in one hour.
#
# A file with NO access_tier line still defers. That is deliberate and matches
# CLAUDE.md: "Only access_tier: owner (or tasks without an access_tier field)
# get full processing." Unknown tier fails CLOSED -- toward yielding.
owner_task_queued() {
  local f tier
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    tier="$(sed -n 's/^access_tier:[[:space:]]*//p' "$f" 2>/dev/null | head -1 | tr -d '[:space:]')"
    case "$tier" in
      team|other|guest|ambient|collaborator) continue ;;   # explicitly not the owner -> ignore
      *) return 0 ;;                    # owner, or unstated -> yield
    esac
  done <<EOF
$(find "$WORKSPACE/tasks" -maxdepth 1 -name 'task-*.txt' ! -name 'task-cron-*.txt' ! -name 'task-workstream-grouping-*.txt' ! -name 'task-project-grouping-*.txt' 2>/dev/null)
EOF
  return 1
}

if [ -d "$WORKSPACE/tasks" ] && owner_task_queued; then
  echo "cron-gate: owner tasks queued — deferring $reason (will retry next fire)"
  exit 0
fi

exec "$@"

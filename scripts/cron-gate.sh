#!/usr/bin/env bash
# Cron prompt gate — defer when owner work is queued.
#
# Wrap each non-loop cron's prompt body so it short-circuits if any owner task
# is still queued in <workspace>/tasks/. This keeps cron noise from competing
# with owner DMs/voice/etc. when /proactive-loop hasn't processed the queue yet.
#
# Usage:
#   bash scripts/cron-gate.sh <reason> <command...>
#   bash scripts/cron-gate.sh --check <reason> [--workspace <dir>]
#
#   - <reason>: short label printed in the deferral message (e.g. "sync-workspace")
#   - <command...>: the actual command to run if the queue is empty
#   - --check: run no command; print one line and exit 0 either way —
#     `deferred: owner tasks queued — <reason> (will retry next fire)` or `clear`.
#     This is the form /schedule-crons prefixes to a sub-daily (or `gate: true`)
#     registered prompt (src/cron_prompt_gate.py), so a prompt-shaped cron can yield too.
#     `--workspace <dir>` checks that workspace's queue (a worker's assigned one).
#
# Example crons.example.json entry (shell form):
#   "prompt": "Run: bash scripts/cron-gate.sh sync-workspace bash scripts/sync-workspace.sh"
#
# Exit codes:
#   0 — either deferred (queue non-empty) OR the wrapped command exited 0
#   Otherwise — propagates the wrapped command's exit code (via exec)
#
# Loop exemption: /proactive-loop MUST NOT be gated — it's the owner-task handler
# itself. Skipping it on a non-empty queue would deadlock the queue.
set -eu

check_only=0
workspace_arg=""
if [ "${1:-}" = "--check" ]; then
  check_only=1
  shift
  if [ $# -eq 3 ] && [ "$2" = "--workspace" ] && [ -n "$3" ]; then
    workspace_arg="$3"
    set -- "$1"
  fi
  if [ $# -ne 1 ]; then
    echo "usage: $0 --check <reason> [--workspace <dir>]" >&2
    exit 2
  fi
elif [ $# -lt 2 ]; then
  echo "usage: $0 <reason> <command...> | $0 --check <reason>" >&2
  exit 2
fi

# Workspace resolution via the canonical M0 helper (PR #1395).
SCRIPT_PARENT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [ -n "$workspace_arg" ]; then
  WORKSPACE="$workspace_arg"
else
  WORKSPACE="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" workspace)"
fi

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
# other / ambient) is peer or public traffic, not the human-owner DMs/voice this
# gate exists to yield to. Deferring on it starves the cron for as long as peers
# keep talking -- observed 2026-08-03: both pending-questions and sync-workspace
# deferred on two #bot2bot notices, with 6 team-tier tasks arriving in one hour.
#
# A file with NO access_tier line still defers. That is deliberate and matches
# CLAUDE.md: "Only access_tier: owner (or tasks without an access_tier field)
# get full processing." Unknown tier fails CLOSED -- toward yielding.
#
# A task a pool worker holds (a sentinel under deliveries/<worker>/, read through
# src/pool_holder.py -- the same optional-skill lookup unanswered-tasks uses) is
# that worker's to answer, not the core's, so it does not hold this gate. A
# worker session's own cron still yields to the tasks held by that worker. An
# unreadable deliveries/ is undecidable, and undecidable yields.
PYBIN="$(bash "$SCRIPT_PARENT/scripts/sutando-config.sh" python-bin 2>/dev/null)" || PYBIN=python3
[ -n "$PYBIN" ] || PYBIN=python3

held_by_another_worker() {
  local holder rc
  holder="$("$PYBIN" "$SCRIPT_PARENT/src/pool_holder.py" "$WORKSPACE" "$1" 2>/dev/null)"; rc=$?
  [ "$rc" -eq 0 ] || return 1
  [ -n "$holder" ] && [ "$holder" != "${SUTANDO_INSTANCE_ID:-}" ]
}

owner_task_queued() {
  local f tier
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    tier="$(sed -n 's/^access_tier:[[:space:]]*//p' "$f" 2>/dev/null | head -1 | tr -d '[:space:]')"
    case "$tier" in
      team|other|guest|ambient) continue ;;   # explicitly not the owner -> ignore
    esac
    held_by_another_worker "$f" && continue   # a worker's task, not this session's
    return 0                                  # owner, or unstated -> yield
  done <<EOF
$(find "$WORKSPACE/tasks" -maxdepth 1 -name 'task-*.txt' ! -name 'task-cron-*.txt' ! -name 'task-workstream-grouping-*.txt' ! -name 'task-project-grouping-*.txt' 2>/dev/null)
EOF
  return 1
}

if [ -d "$WORKSPACE/tasks" ] && owner_task_queued; then
  if [ "$check_only" -eq 1 ]; then
    echo "deferred: owner tasks queued — $reason (will retry next fire)"
  else
    echo "cron-gate: owner tasks queued — deferring $reason (will retry next fire)"
  fi
  exit 0
fi

if [ "$check_only" -eq 1 ]; then
  echo "clear"
  exit 0
fi

exec "$@"

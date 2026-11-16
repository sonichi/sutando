#!/usr/bin/env bash
# SessionStart hook — reminds the core agent (and, separately, a pool worker)
# to (re-)run its own startup bootstrap at the start of every session,
# including post-compaction restarts.
#
# /startup is the canonical fresh-session bootstrap for the core: it runs
# task-orphan recovery, THEN registers crons (/schedule-crons), THEN starts
# the watcher. /startup --worker is the worker equivalent: it starts the
# worker's own watcher and, on a fresh sentinel state, registers any
# crons.json entries pinned to that worker's own instance id (per-worker
# cron ownership; see skills/startup/SKILL.md "Worker mode"). Claude Code
# crons are session-only: they expire when the session ends, and nothing
# else re-creates them.
#
# SCOPE GATE: originally core-only (SUTANDO_CORE_SESSION=1, set by the core
# launcher). A pool worker was NOT covered — reasoned at the time as an
# "ad-hoc session in the same checkout" alongside PR-review/codex sessions,
# which was wrong: a worker is long-lived with its own pinned session-only
# crons, architecturally closer to the core than to a one-shot reviewer.
# Confirmed live (2026-09-28): a worker's comm-sweep cron vanished at a
# context-compaction boundary and stayed dead 22.6h with no alert, because
# nothing ever prompted that worker to re-run its own startup bootstrap.
# Detect a worker via $SUTANDO_WORKER_BOOTSTRAP (set by the spawner, named in
# skills/startup/SKILL.md step 1) rather than $SUTANDO_INSTANCE_ID — the
# bootstrap-script marker is worker-specific; an instance id is not verified
# absent on core. Fail-open is still deliberately NOT used for the "neither"
# case: a plain `claude` in the checkout (PR review, codex, ad hoc) carries
# neither marker and stays silent (exit 0, no context).
#
# The hook outputs additionalContext that Claude Code injects into the fresh
# context window. Best-effort: any failure exits 0 so the hook never blocks
# a session start.

set -euo pipefail

if [ "${SUTANDO_CORE_SESSION:-}" = "1" ]; then
  printf '%s' '{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "SUTANDO STARTUP: Run /startup immediately — it recovers orphan tasks, registers all 16 session-only crons (proactive loop, morning briefing, digests, etc.), and starts the streaming task watcher. Crons are session-only and unregistered in this fresh session. Do this before any other work."
  }
}'
elif [ -n "${SUTANDO_WORKER_BOOTSTRAP:-}" ]; then
  printf '%s' '{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "SUTANDO WORKER STARTUP: Run /startup --worker immediately — it starts this worker'"'"'s own task watcher and, on a fresh boot, registers any crons.json entries pinned to this worker'"'"'s own instance id. Those crons are session-only and do NOT survive a session restart or context compaction; nothing else re-registers them. Do this before any other work, including right after a compaction."
  }
}'
else
  # Neither marker present (PR review, codex, a plain `claude` in the
  # checkout) — see SCOPE GATE above.
  exit 0
fi

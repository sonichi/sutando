#!/usr/bin/env bash
# SessionStart hook — reminds the core agent to run /startup at the
# start of every session (including post-compaction restarts).
#
# /startup is the canonical fresh-session bootstrap: it runs task-orphan
# recovery, THEN registers crons (/schedule-crons), THEN starts the watcher.
# Claude Code crons are session-only: they expire when the session ends.
# Without an explicit /startup invocation, recurring jobs (proactive loop,
# morning briefing, digests, etc.) never fire for that session.
#
# SCOPE GATE: only the long-lived sutando-core session should bootstrap. The
# core launcher (src/agent/claude/cli/start-cli.sh) sets SUTANDO_CORE_SESSION=1
# for the core process; ad-hoc sessions in the same checkout (PR review, codex,
# a plain `claude` in the repo) do not have it, so they get no reminder and
# never trigger a redundant cron bootstrap. Fail-open is deliberately NOT used:
# when the marker is absent we stay silent (exit 0, no context) rather than
# fire for every session.
#
# The hook outputs additionalContext that Claude Code injects into the fresh
# context window. Best-effort: any failure exits 0 so the hook never blocks
# a session start.

set -euo pipefail

# Only the core session bootstraps — see SCOPE GATE above.
if [ "${SUTANDO_CORE_SESSION:-}" != "1" ]; then
  exit 0
fi

printf '%s' '{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "SUTANDO STARTUP: Run /startup immediately — it recovers orphan tasks, registers all 16 session-only crons (proactive loop, morning briefing, digests, etc.), and starts the streaming task watcher. Crons are session-only and unregistered in this fresh session. Do this before any other work."
  }
}'

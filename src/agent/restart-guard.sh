#!/bin/bash
# Shared policy for both runtime launchers: a `--restart` issued from inside the
# core session kill-sessions the very agent running the command.

# Callers MUST pass the marker snapshotted BEFORE their own
# `export SUTANDO_CORE_SESSION=1`; the live value is always 1 by then.
sutando_restart_guard_refuses() {
  [ "${1:-}" = "1" ] || return 1
  [ "${SUTANDO_ALLOW_INSESSION_RESTART:-}" != "1" ] || return 1
  return 0
}

SUTANDO_RESTART_GUARD_REASON="refused: inherited SUTANDO_CORE_SESSION=1 (in-session self-kill), no override set"

# One line per restart attempt in <workspace>/logs/restart-attempts.log: $1 repo, $2 kind, $3 message.
# Best effort: an unresolvable workspace or unwritable log never fails the caller.
sutando_restart_attempt_log() {
  local ws
  ws="$(bash "$1/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mkdir -p "$ws/logs" 2>/dev/null || true
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" "$3" >> "$ws/logs/restart-attempts.log" 2>/dev/null || true
}

sutando_restart_guard_explain() {
  {
    echo "start-cli: refusing --restart from inside the sutando-core session."
    echo "  kill-session would terminate the agent that is running this command."
    echo "  Use one of these instead:"
    echo "    1. owner types 'restart core' in chat -> the bridge writes a restart"
    echo "       intent that Sutando.app consumes and relaunches in the GUI login session."
    echo "    2. dead core: the launchd health-check fallback, but ONLY where that job"
    echo "       runs with --recover-core. It is absent from the shipped default args"
    echo "       (#2246), so on a stock install this option recovers nothing."
    echo "    3. a human in a terminal OUTSIDE the core, or the Sutando.app menu."
    echo "  NOT --emit-task: that queues work for the core to consume, and a core"
    echo "  that needs restarting is exactly the one that cannot consume it."
    echo "  Out-of-session automation launched from a core shell inherits"
    echo "  SUTANDO_CORE_SESSION; it can override with SUTANDO_ALLOW_INSESSION_RESTART=1."
  } >&2
}

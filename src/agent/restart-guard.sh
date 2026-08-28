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

sutando_restart_guard_explain() {
  {
    echo "start-cli: refusing --restart from inside the sutando-core session."
    echo "  kill-session would terminate the agent that is running this command."
    echo "  Use one of these instead:"
    echo "    1. owner types 'restart core' in chat -> the bridge writes a restart"
    echo "       intent that Sutando.app consumes and relaunches in the GUI login session."
    echo "    2. dead core: the launchd health-check fallback recovers it out-of-session."
    echo "    3. a human in a terminal OUTSIDE the core, or the Sutando.app menu."
    echo "  NOT --emit-task: that queues work for the core to consume, and a core"
    echo "  that needs restarting is exactly the one that cannot consume it."
    echo "  Out-of-session automation launched from a core shell inherits"
    echo "  SUTANDO_CORE_SESSION; it can override with SUTANDO_ALLOW_INSESSION_RESTART=1."
  } >&2
}

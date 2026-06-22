#!/usr/bin/env bash
# Bounded runner for sandboxed codex (or any) delegation: run a command under a
# HARD wall-clock deadline and hard-kill its whole process TREE if it overruns —
# so a `codex exec --sandbox read-only` delegation can never grind unbounded and
# need a manual kill (the 2026-06-22 PR-review degrade-crawl). No `gtimeout`
# dependency (not installed on macOS); pure bash + pgrep tree-walk.
#
#   bash scripts/codex-bounded.sh <deadline_secs> -- <command...>
#
# Exit: forwards the command's own exit code on completion; 124 if it was killed
# on the deadline. Always redirect codex's stdin from /dev/null at the call site
# (`< /dev/null`) — a backgrounded codex otherwise waits on open stdin forever.
set -u

DEADLINE="${1:?usage: codex-bounded.sh <secs> -- <cmd...>}"; shift
[[ "${1:-}" == "--" ]] && shift
[[ $# -ge 1 ]] || { echo "codex-bounded: no command given" >&2; exit 2; }

# Recursively kill a process and all its descendants (macOS-safe: pgrep -P).
_kill_tree() {
    local p="$1" sig="$2" k
    for k in $(pgrep -P "$p" 2>/dev/null); do _kill_tree "$k" "$sig"; done
    kill "-$sig" "$p" 2>/dev/null
}

"$@" &
CMD_PID=$!

# Watchdog: after the deadline, if the command is still alive, TERM then KILL its
# whole tree. Runs in a subshell so the main path can just `wait`.
(
    _slept=0
    while (( _slept < DEADLINE )); do
        kill -0 "$CMD_PID" 2>/dev/null || exit 0   # finished early → nothing to do
        sleep 2; _slept=$(( _slept + 2 ))
    done
    if kill -0 "$CMD_PID" 2>/dev/null; then
        _kill_tree "$CMD_PID" TERM
        sleep 2
        _kill_tree "$CMD_PID" KILL
    fi
) &
WATCHER=$!

wait "$CMD_PID" 2>/dev/null; rc=$?
kill "$WATCHER" 2>/dev/null      # command finished first → stop the watchdog
wait "$WATCHER" 2>/dev/null || true

# A command killed by a signal exits >128; treat that as a deadline kill.
if (( rc > 128 )); then
    echo "codex-bounded: killed on ${DEADLINE}s deadline" >&2
    exit 124
fi
exit "$rc"

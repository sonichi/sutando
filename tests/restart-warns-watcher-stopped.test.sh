#!/usr/bin/env bash
# restart.sh kills the task watcher and NOTHING can bring it back: the watcher is
# armed by the agent via the Monitor tool (schedule-crons step 1.5), and startup.sh
# carries no `watch-tasks` reference at all. Every other STOP_PATTERNS entry is
# relaunched or reported; this one used to die quietly while the caller was told
# the restart succeeded.
#
# Measured 2026-09-11: an owner-authorized restart stopped the watcher on a live
# core. It was only noticed because the Monitor surfaced an exit-144 notification —
# a harness artifact, not something restart.sh provides. health-check does warn
# ("watcher not running (no PID sentinel)"), but only on the next proactive pass;
# this closes the gap at the moment of the kill.
#
# Run: bash tests/restart-warns-watcher-stopped.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RS="$REPO/src/restart.sh"
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

grep -q 'pkill -f "watch-tasks"' "$RS"; ck "restart.sh still stops the watcher (guards the rest)" $?

# The warning must sit AFTER the pkill: printed before, it describes a watcher
# that is still running.
kill_line=$(grep -n 'pkill -f "watch-tasks"' "$RS" | head -1 | cut -d: -f1)
warn_line=$(grep -n 'task watcher STOPPED' "$RS" | head -1 | cut -d: -f1)
[ -n "$warn_line" ]; ck "a watcher-stopped warning exists" $?
[ -n "$warn_line" ] && [ "$warn_line" -gt "$kill_line" ]; ck "the warning follows the kill, not precedes it" $?

grep -q 'watch-tasks-stream.sh' "$RS"; ck "the warning names the command that re-arms it" $?

# REMOVED: `! grep -q "watch-tasks" startup.sh`. It asserted a SUBSTRING, not the
# premise. Measured both directions: inlining startup.sh's sentinel path (a no-op
# refactor, and the form a shipped engine build carries) made it FAIL, while a real
# re-arm written as `__w="watch-""tasks-stream.sh"` left it passing. It fired on no
# change and stayed silent on the regression it guarded. A sound replacement must run
# startup.sh, which `set -e` plus ~82 env dependencies make a partial run — and a
# partial run's silence is not evidence. Tracked rather than replaced by a second proxy.

bash -n "$RS"; ck "restart.sh parses" $?

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }

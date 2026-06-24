#!/usr/bin/env bash
# Bounded runner for sandboxed codex (or any) delegation. Two guards:
#
#   --stall N   STALL WATCHDOG (the important one): kill the command's whole
#               process tree if it emits NO stdout/stderr for N seconds. This is
#               the "is it ever going to finish?" signal — a healthy codex review
#               streams reasoning/tool events as it works, so N seconds of total
#               silence means it's wedged, not merely slow. We do NOT want to kill
#               a slow-but-progressing review; we DO want to kill a dead one.
#   --max  M    ABSOLUTE BACKSTOP: hard wall-clock cap. Even a pathological
#               steady-trickle can't run past M seconds. `--max 0` DISABLES the
#               cap (stall-only mode); default when unset is 900.
#
# Exit codes: forwards the command's own code on normal completion; 125 if killed
# as STALLED (no output for --stall s); 124 if killed on the --max cap.
#
#   bash skills/claude-codex/scripts/codex-bounded.sh --stall 90 --max 900 -- <command...>
#
# Back-compat: a bare leading integer is treated as --max with stall disabled,
# so the historical `codex-bounded.sh 120 -- <cmd>` form still means "pure 120s
# deadline, exit 124 on overrun" — unchanged.
#
# Always redirect codex's stdin from /dev/null at the call site (`< /dev/null`) —
# a backgrounded codex otherwise waits on open stdin forever. No `gtimeout`
# dependency (not on macOS); pure bash + pgrep tree-walk.
set -u

STALL=0          # 0 = stall watchdog disabled (pure --max deadline)
MAX=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --stall) STALL="${2:?--stall needs a value}"; shift 2;;
        --max)   MAX="${2:?--max needs a value}";     shift 2;;
        --)      shift; break;;
        [0-9]*)  MAX="$1"; shift;;                 # back-compat positional deadline
        *)       break;;
    esac
done
[[ -n "$MAX" ]] || MAX=900
[[ $# -ge 1 ]] || { echo "codex-bounded: no command given" >&2; exit 2; }

# Portable mtime (BSD/macOS `stat -f`, GNU/Linux `stat -c`).
_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null; }

# Recursively kill a process and all its descendants (macOS-safe: pgrep -P).
_kill_tree() {
    local p="$1" sig="$2" k
    for k in $(pgrep -P "$p" 2>/dev/null); do _kill_tree "$k" "$sig"; done
    kill "-$sig" "$p" 2>/dev/null
}

# Progress sink: tee the command's stdout AND stderr through to our own fds
# (pass-through unchanged) while also appending to OUTFILE, whose mtime is the
# liveness signal the watchdog samples.
# Granularity note (Maddy review 2026-06-23): mtime advances when `tee` flushes a
# LINE, so "stall" means "no flushed line for N s" — flush-granular, not byte-
# granular. Fine for codex (it streams newline-terminated events); the only blind
# spot is a process that byte-trickles with NO newline for >N s, which `--max` caps.
OUTFILE="$(mktemp -t codex-bounded.XXXXXX)"
VERDICT="$OUTFILE.verdict"
: > "$OUTFILE"
"$@" > >(tee -a "$OUTFILE") 2> >(tee -a "$OUTFILE" >&2) &
CMD_PID=$!

# Watchdog subshell: samples elapsed-time (--max) and output-staleness (--stall).
(
    start=$(date +%s)
    while kill -0 "$CMD_PID" 2>/dev/null; do
        now=$(date +%s)
        if (( MAX > 0 && now - start >= MAX )); then echo MAX > "$VERDICT"; break; fi
        if (( STALL > 0 )); then
            mt=$(_mtime "$OUTFILE"); mt=${mt:-$start}
            if (( now - mt >= STALL )); then echo STALL > "$VERDICT"; break; fi
        fi
        sleep 2
    done
    if kill -0 "$CMD_PID" 2>/dev/null; then
        _kill_tree "$CMD_PID" TERM
        sleep 2
        _kill_tree "$CMD_PID" KILL
    fi
) &
WATCHER=$!

# If the WRAPPER itself is interrupted/killed (SIGINT/TERM/HUP) or exits for any
# reason, tear down BOTH the watchdog and the delegated command tree — otherwise an
# external kill of this script orphans the codex tree and defeats the whole bounding
# guarantee. Idempotent: on the normal path CMD_PID has already exited (no-op) and
# WATCHER is killed below. Also sweeps the temp files.
_cleanup() { _kill_tree "$CMD_PID" KILL 2>/dev/null; kill "$WATCHER" 2>/dev/null; rm -f "$OUTFILE" "$VERDICT"; }
trap '_cleanup' EXIT
trap '_cleanup; exit 130' INT
trap '_cleanup; exit 143' TERM HUP

wait "$CMD_PID" 2>/dev/null; rc=$?
kill "$WATCHER" 2>/dev/null      # command finished first → stop the watchdog
wait "$WATCHER" 2>/dev/null || true

verdict="$(cat "$VERDICT" 2>/dev/null || true)"
rm -f "$OUTFILE" "$VERDICT"

case "$verdict" in
    STALL) echo "codex-bounded: STALLED — no output for ${STALL}s, killed (not going to finish)" >&2; exit 125;;
    MAX)   echo "codex-bounded: hit ${MAX}s absolute cap, killed" >&2; exit 124;;
esac
# verdict empty + rc>128 = the command died from an EXTERNAL signal — our watchdog
# always writes a verdict (STALL/MAX) BEFORE it kills, so reaching here means the
# kill wasn't ours. Forward the real signal-exit code; don't claim a deadline we
# didn't enforce. (Non-zero still trips the bridge's Stage-2 fallback.)
if (( rc > 128 )); then
    echo "codex-bounded: command killed by signal (exit $rc, not our watchdog)" >&2
    exit "$rc"
fi
exit "$rc"

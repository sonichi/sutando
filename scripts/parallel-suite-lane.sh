#!/usr/bin/env bash
# One SERIAL worker per git worktree: worker w takes lines w, w+W, w+2W... so a
# worktree never holds two suites at once — exclusivity is structural, not a lock.
# usage: parallel-suite-lane.sh <workers> <files-list> <recdir> <cmd-prefix...>
# Records land as <recdir>/<line-index>.{out,rc}; aggregation stays the caller's.
set -uo pipefail
WORKERS="$1"; FILES="$2"; RECDIR="$3"; shift 3
N="$(wc -l < "$FILES" | tr -d ' ')"

WTDIR="$(mktemp -d)"
for _w in $(seq 1 "$WORKERS"); do
    git worktree add --detach -q "$WTDIR/$_w" HEAD
done
_lane_cleanup() {
    for _w in $(seq 1 "$WORKERS"); do
        git worktree remove --force "$WTDIR/$_w" 2>/dev/null || true
    done
    rm -rf "$WTDIR"
}
trap _lane_cleanup EXIT

for _w in $(seq 1 "$WORKERS"); do
    (
        idx="$_w"
        while [ "$idx" -le "$N" ]; do
            f="$(sed -n "${idx}p" "$FILES")"
            rec="$RECDIR/$idx"
            out="$(cd "$WTDIR/$_w" && "$@" "$f" 2>&1)" && rc=0 || rc=$?
            printf "%s" "$out" > "$rec.out"
            printf "%s\n" "$rc" > "$rec.rc"
            idx=$((idx + WORKERS))
        done
    ) &
done
wait

# Instrumented runs write .coverage.* inside the worktrees; `coverage combine`
# searches the caller's cwd, so bring the fragments home (no-op when none).
find "$WTDIR" -maxdepth 2 -name '.coverage.*' -exec mv {} . \; 2>/dev/null || true

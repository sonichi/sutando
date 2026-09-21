#!/usr/bin/env bash
# One SERIAL worker per git worktree, all workers pulling from one queue: a
# worktree never holds two suites at once, and no worker idles while another
# still has a pile — a fixed stride left the heaviest suites stacked on one worker.
# usage: parallel-suite-lane.sh <workers> <files-list> <recdir> <cmd-prefix...>
# Records land as <recdir>/<line-index>.{out,rc,time}; aggregation stays the caller's.
set -uo pipefail
WORKERS="$1"; FILES="$2"; RECDIR="$3"; shift 3
N="$(wc -l < "$FILES" | tr -d ' ')"

# No dot in the lane path: a suite that derives a project slug from its checkout
# path would otherwise disagree with the slug the product computes.
WTDIR="$(mktemp -d "${TMPDIR:-/tmp}/lane-XXXXXX")"
for _w in $(seq 1 "$WORKERS"); do
    git worktree add --detach -q "$WTDIR/$_w" HEAD
    # Installed deps are untracked, so a fresh worktree has none; share the caller's.
    [ -d node_modules ] && ln -s "$PWD/node_modules" "$WTDIR/$_w/node_modules"
done
_lane_cleanup() {
    for _w in $(seq 1 "$WORKERS"); do
        git worktree remove --force "$WTDIR/$_w" 2>/dev/null || true
    done
    rm -rf "$WTDIR" "$RECDIR"/.claim-*
}
trap _lane_cleanup EXIT

# The next suite starts from a clean tree, whichever suite ran there before.
# Ignored files stay: the shared node_modules link and coverage fragments.
# Only ever the lane worktree itself: an exported GIT_DIR/GIT_WORK_TREE would
# otherwise point `git -C` at the caller's checkout, and a reset there wipes work.
_lane_reset() {
    local top
    top="$(cd "$1" 2>/dev/null && env -u GIT_DIR -u GIT_WORK_TREE git rev-parse --show-toplevel 2>/dev/null)" || return 0
    [ "$top" = "$(cd "$1" && pwd -P)" ] || return 0
    env -u GIT_DIR -u GIT_WORK_TREE git -C "$1" checkout -q -- . 2>/dev/null || true
    # The node_modules link is a symlink, which `node_modules/` in .gitignore does
    # not cover (that pattern matches directories only), so exclude it by name.
    env -u GIT_DIR -u GIT_WORK_TREE git -C "$1" clean -fdq -e node_modules 2>/dev/null || true
}

for _w in $(seq 1 "$WORKERS"); do
    (
        for idx in $(seq 1 "$N"); do
            # mkdir is the atomic claim: exactly one worker creates it, the rest skip.
            mkdir "$RECDIR/.claim-$idx" 2>/dev/null || continue
            f="$(sed -n "${idx}p" "$FILES")"
            rec="$RECDIR/$idx"
            _t0=$SECONDS
            out="$(cd "$WTDIR/$_w" && "$@" "$f" 2>&1 < /dev/null)" && rc=0 || rc=$?
            printf "%s" "$out" > "$rec.out"
            printf "%s\n" "$rc" > "$rec.rc"
            printf "%s\n" "$(( SECONDS - _t0 ))" > "$rec.time"
            _lane_reset "$WTDIR/$_w"
        done
    ) &
done
wait

# Instrumented runs write .coverage.* inside the worktrees; `coverage combine`
# searches the caller's cwd, so bring the fragments home (no-op when none).
find "$WTDIR" -maxdepth 2 -name '.coverage.*' -exec mv {} . \; 2>/dev/null || true

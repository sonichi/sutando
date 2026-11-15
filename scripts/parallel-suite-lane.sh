#!/usr/bin/env bash
# One SERIAL worker per git worktree, all workers pulling from one queue: a
# worktree never holds two suites at once, and no worker idles while another
# still has a pile — a fixed stride left the heaviest suites stacked on one worker.
# usage: parallel-suite-lane.sh <workers> <files-list> <recdir> <cmd-prefix...>
# Records land as <recdir>/<line-index>.{out,rc,time}; aggregation stays the caller's.
set -uo pipefail
WORKERS="$1"; FILES="$2"; RECDIR="$3"; shift 3
N="$(wc -l < "$FILES" | tr -d ' ')"
REVISION="$(git rev-parse --verify HEAD)" || exit 1
LANE_GIT_DIR="$(git rev-parse --absolute-git-dir)" || exit 1
LANE_GIT_ENV="$(git rev-parse --local-env-vars)" || exit 1
# Discover the caller's repository first, then isolate every lane Git operation
# and suite from inherited repository selectors, including an external index.
while IFS= read -r _git_var; do unset "$_git_var"; done <<< "$LANE_GIT_ENV"

# No dot in the lane path: a suite that derives a project slug from its checkout
# path would otherwise disagree with the slug the product computes.
WTDIR="$(mktemp -d "${TMPDIR:-/tmp}/lane-XXXXXX")" || exit 1
_lane_cleanup() {
    for _w in $(seq 1 "$WORKERS"); do
        git --git-dir="$LANE_GIT_DIR" worktree remove --force "$WTDIR/$_w" 2>/dev/null || true
    done
    rm -rf "$WTDIR" "$RECDIR"/.claim-*
}
trap _lane_cleanup EXIT
for _w in $(seq 1 "$WORKERS"); do
    git --git-dir="$LANE_GIT_DIR" worktree add --detach -q "$WTDIR/$_w" "$REVISION" || exit 1
    # Installed deps are untracked, so a fresh worktree has none; share the caller's.
    if [ -d node_modules ]; then
        ln -s "$PWD/node_modules" "$WTDIR/$_w/node_modules" || exit 1
    fi
done

# The next suite starts from a clean tree, whichever suite ran there before.
# Ignored files stay: the shared node_modules link and coverage fragments.
_lane_reset() {
    local top
    top="$(cd "$1" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" || return 1
    [ "$top" = "$(cd "$1" && pwd -P)" ] || return 1
    # Detach before restoring the pinned commit: a suite may have selected a
    # shared branch, which recovery must not move.
    git -C "$1" checkout -q --detach --force "$REVISION" || return 1
    # The node_modules link is a symlink, which `node_modules/` in .gitignore does
    # not cover (that pattern matches directories only), so exclude it by name.
    git -C "$1" clean -fdq -e node_modules
}

pids=()
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
            if ! _lane_reset "$WTDIR/$_w"; then
                echo "parallel-suite-lane: cannot restore worker $_w after suite $idx; stopping worker" >&2
                exit 1
            fi
        done
    ) &
    pids+=("$!")
done
failed=0
for _pid in "${pids[@]}"; do
    wait "$_pid" || failed=1
done

# Instrumented runs write .coverage.* inside the worktrees; `coverage combine`
# searches the caller's cwd, so bring the fragments home (no-op when none).
find "$WTDIR" -maxdepth 2 -name '.coverage.*' -exec mv {} . \; 2>/dev/null || true
exit "$failed"

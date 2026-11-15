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
LANE_COMMON_DIR="$(git rev-parse --git-common-dir)" || exit 1
LANE_COMMON_DIR="$(cd "$LANE_COMMON_DIR" && pwd -P)" || exit 1
CALLER_ROOT="$(git rev-parse --show-toplevel)" || exit 1
CALLER_ROOT="$(cd "$CALLER_ROOT" && pwd -P)" || exit 1
CALLER_INDEX="$(git rev-parse --git-path index)" || exit 1
case "$CALLER_INDEX" in /*) ;; *) CALLER_INDEX="$PWD/$CALLER_INDEX" ;; esac
LANE_GIT_ENV="$(git rev-parse --local-env-vars)" || exit 1
# Discover the caller's repository first, then isolate every lane Git operation
# and suite from inherited repository selectors, including an external index.
while IFS= read -r _git_var; do unset "$_git_var"; done <<< "$LANE_GIT_ENV"

# No dot in the lane path: a suite that derives a project slug from its checkout
# path would otherwise disagree with the slug the product computes.
WTDIR="$(mktemp -d "${TMPDIR:-/tmp}/lane-XXXXXX")" || exit 1
WTDIR="$(cd "$WTDIR" && pwd -P)" || exit 1
worker_dirs=(); worker_gitdirs=(); worker_gitfiles=(); worker_commondirs=(); worker_backlinks=()
worker_configured_roots=()
pids=(); CREATED=0; WORKERS_JOINED=0

_lane_git() {
    GIT_COMMON_DIR="$LANE_COMMON_DIR" git --git-dir="$LANE_GIT_DIR" \
        --work-tree="$CALLER_ROOT" -c core.worktree="$CALLER_ROOT" -c core.hooksPath=/dev/null "$@"
}

_lane_identity() {
    local id="$1" worker admin top index common configured_root
    worker="${worker_dirs[$id]-}"; admin="${worker_gitdirs[$id]-}"
    [ -n "$worker" ] && [ -n "$admin" ] || return 1
    [ ! -L "$WTDIR" ] && [ "$(cd "$WTDIR" 2>/dev/null && pwd -P)" = "$WTDIR" ] || return 1
    [ ! -L "$worker" ] && [ "$(cd "$worker" 2>/dev/null && pwd -P)" = "$worker" ] || return 1
    [ "$worker" != "$CALLER_ROOT" ] && [ ! "$worker" -ef "$CALLER_ROOT" ] || return 1
    [ ! -L "$worker/.git" ] && [ -f "$worker/.git" ] || return 1
    [ "$(cat "$worker/.git")" = "${worker_gitfiles[$id]}" ] || return 1
    [ ! -L "$admin" ] && [ "$(cd "$admin" 2>/dev/null && pwd -P)" = "$admin" ] || return 1
    [ "$admin" != "$LANE_GIT_DIR" ] && [ "$admin" != "$LANE_COMMON_DIR" ] || return 1
    [ ! -L "$admin/commondir" ] && [ ! -L "$admin/gitdir" ] && [ ! -L "$admin/index" ] || return 1
    [ "$(cat "$admin/commondir")" = "${worker_commondirs[$id]}" ] || return 1
    [ "$(cat "$admin/gitdir")" = "${worker_backlinks[$id]}" ] || return 1
    [ ! "$admin/index" -ef "$CALLER_INDEX" ] || return 1
    [ ! "$admin/index" -ef "$LANE_GIT_DIR/index" ] || return 1
    [ "$(git -C "$worker" rev-parse --absolute-git-dir 2>/dev/null)" = "$admin" ] || return 1
    configured_root="$(git -C "$worker" config --get core.worktree)" || configured_root=""
    [ "$configured_root" = "${worker_configured_roots[$id]}" ] || return 1
    top="$(git -C "$worker" rev-parse --show-toplevel 2>/dev/null)" || return 1
    [ "$top" = "$worker" ] || return 1
    index="$(git -C "$worker" rev-parse --git-path index 2>/dev/null)" || return 1
    [ "$index" = "$admin/index" ] || return 1
    common="$(git -C "$worker" rev-parse --git-common-dir 2>/dev/null)" || return 1
    [ "$(cd "$common" 2>/dev/null && pwd -P)" = "$LANE_COMMON_DIR" ]
}

_lane_cancel() {
    local pid remaining attempt
    for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM -- "-$pid" 2>/dev/null || true; done
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        remaining=0
        for pid in ${pids[@]+"${pids[@]}"}; do
            kill -0 -- "-$pid" 2>/dev/null && remaining=1
        done
        [ "$remaining" = 1 ] || break
        sleep 0.1
    done
    for pid in ${pids[@]+"${pids[@]}"}; do kill -KILL -- "-$pid" 2>/dev/null || true; done
    for pid in ${pids[@]+"${pids[@]}"}; do wait "$pid" 2>/dev/null || true; done
}

_lane_cleanup() {
    local status="$?" id unsafe=0
    trap '' INT TERM HUP
    if [ "$WORKERS_JOINED" = 0 ]; then _lane_cancel; fi
    for id in $(seq 1 "$CREATED"); do
        if _lane_identity "$id"; then
            _lane_git worktree remove --force "${worker_dirs[$id]}" 2>/dev/null || { unsafe=1; status=1; }
        else
            echo "parallel-suite-lane: unsafe worker $id identity; retaining worker files" >&2
            unsafe=1; status=1
        fi
    done
    if [ "$unsafe" = 0 ]; then rm -rf "$WTDIR"; fi
    rmdir "$RECDIR"/.claim-* 2>/dev/null || true
    trap - EXIT
    exit "$status"
}
trap _lane_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
for _w in $(seq 1 "$WORKERS"); do
    _lane_git worktree add --detach -q "$WTDIR/$_w" "$REVISION" || exit 1
    CREATED="$_w"
    worker_dirs[$_w]="$WTDIR/$_w"
    worker_gitdirs[$_w]="$(git -C "$WTDIR/$_w" rev-parse --absolute-git-dir)" || exit 1
    worker_gitfiles[$_w]="$(cat "$WTDIR/$_w/.git")" || exit 1
    worker_commondirs[$_w]="$(cat "${worker_gitdirs[$_w]}/commondir")" || exit 1
    worker_backlinks[$_w]="$(cat "${worker_gitdirs[$_w]}/gitdir")" || exit 1
    worker_configured_roots[$_w]="$(git -C "$WTDIR/$_w" config --get core.worktree)" || worker_configured_roots[$_w]=""
    # Installed deps are untracked, so a fresh worktree has none; share the caller's.
    if [ -d node_modules ]; then
        ln -s "$PWD/node_modules" "$WTDIR/$_w/node_modules" || exit 1
    fi
done

# The next suite starts from a clean tree, whichever suite ran there before.
# Ignored files stay: the shared node_modules link and coverage fragments.
_lane_reset() {
    local worker="${worker_dirs[$1]}" admin="${worker_gitdirs[$1]}"
    _lane_identity "$1" || return 1
    # Detach before restoring the pinned commit: a suite may have selected a
    # shared branch, which recovery must not move.
    GIT_COMMON_DIR="$LANE_COMMON_DIR" GIT_INDEX_FILE="$admin/index" \
        git --git-dir="$admin" --work-tree="$worker" -c core.worktree="$worker" \
        -c core.hooksPath=/dev/null -c core.fsmonitor=false -c submodule.recurse=false \
        checkout -q --detach --force "$REVISION" || return 1
    # The node_modules link is a symlink, which `node_modules/` in .gitignore does
    # not cover (that pattern matches directories only), so exclude it by name.
    _lane_identity "$1" || return 1
    GIT_COMMON_DIR="$LANE_COMMON_DIR" GIT_INDEX_FILE="$admin/index" \
        git --git-dir="$admin" --work-tree="$worker" -c core.worktree="$worker" \
        -c core.fsmonitor=false clean -fdq -e node_modules
}

# Job control gives each owned worker a process group, including its suites.
set -m
for _w in $(seq 1 "$WORKERS"); do
    (
        set +m
        trap - EXIT INT TERM HUP
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
            if ! _lane_reset "$_w"; then
                echo "parallel-suite-lane: cannot restore worker $_w after suite $idx; stopping worker" >&2
                exit 1
            fi
        done
    ) &
    pids+=("$!")
done
set +m
failed=0
for _job in "${!pids[@]}"; do
    wait "${pids[$_job]}" || failed=1
    unset 'pids[_job]'
done
WORKERS_JOINED=1

# Instrumented runs write .coverage.* inside the worktrees; `coverage combine`
# searches the caller's cwd, so bring the fragments home (no-op when none).
find "$WTDIR" -maxdepth 2 -name '.coverage.*' -exec mv {} . \; 2>/dev/null || true
exit "$failed"

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
worker_configured_roots=(); worker_inodes=(); worker_admin_inodes=()
pids=(); CREATED=0; WORKERS_JOINED=0
SPAWNING=0; SIGNAL_STATUS=0

_lane_inode() {
    python3 -c 'import os, sys; s = os.stat(sys.argv[1], follow_symlinks=False); print("%s:%s" % (s.st_dev, s.st_ino))' "$1" 2>/dev/null
}
WTDIR_INODE="$(_lane_inode "$WTDIR")" || exit 1

_lane_signal() {
    if [ "$SPAWNING" = 1 ]; then SIGNAL_STATUS="$1"; else exit "$1"; fi
}

_lane_spawn_done() {
    SPAWNING=0
    [ "$SIGNAL_STATUS" = 0 ] || exit "$SIGNAL_STATUS"
}

_lane_git() {
    GIT_COMMON_DIR="$LANE_COMMON_DIR" git --git-dir="$LANE_GIT_DIR" \
        --work-tree="$CALLER_ROOT" -c core.worktree="$CALLER_ROOT" -c core.hooksPath=/dev/null "$@"
}

_lane_identity() {
    local id="$1" worker admin top index common configured_root
    worker="${worker_dirs[$id]-}"; admin="${worker_gitdirs[$id]-}"
    [ -n "$worker" ] && [ -n "$admin" ] || return 1
    [ ! -L "$WTDIR" ] && [ "$(cd "$WTDIR" 2>/dev/null && pwd -P)" = "$WTDIR" ] || return 1
    [ "$(_lane_inode "$WTDIR")" = "$WTDIR_INODE" ] || return 1
    [ ! -L "$worker" ] && [ "$(cd "$worker" 2>/dev/null && pwd -P)" = "$worker" ] || return 1
    [ "$(_lane_inode "$worker")" = "${worker_inodes[$id]-}" ] || return 1
    [ "$worker" != "$CALLER_ROOT" ] && [ ! "$worker" -ef "$CALLER_ROOT" ] || return 1
    [ ! -L "$worker/.git" ] && [ -f "$worker/.git" ] || return 1
    [ "$(cat "$worker/.git")" = "${worker_gitfiles[$id]}" ] || return 1
    [ ! -L "$admin" ] && [ "$(cd "$admin" 2>/dev/null && pwd -P)" = "$admin" ] || return 1
    [ "$(_lane_inode "$admin")" = "${worker_admin_inodes[$id]-}" ] || return 1
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

_lane_group_live() {
    kill -0 -- "-$1" 2>/dev/null || return 1
    [ -d /proc/self ] || return 0
    python3 - "$1" <<'PY'
import os
import sys

group = int(sys.argv[1])
try:
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open('/proc/' + entry + '/stat') as stream:
                data = stream.read()
        except (FileNotFoundError, ProcessLookupError):
            continue
        fields = data[data.rfind(')') + 2:].split()
        if int(fields[2]) == group and fields[0] not in ('Z', 'X'):
            sys.exit(0)
except (OSError, ValueError, IndexError):
    sys.exit(2)
sys.exit(1)
PY
}

_lane_stop_group() {
    local group="$1" attempt state
    kill -TERM -- "-$group" 2>/dev/null || true
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        _lane_group_live "$group"; state=$?
        [ "$state" != 1 ] || return 0
        [ "$state" = 0 ] || break
        sleep 0.1
    done
    kill -KILL -- "-$group" 2>/dev/null || true
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        _lane_group_live "$group"; state=$?
        [ "$state" != 1 ] || return 0
        sleep 0.1
    done
    return 1
}

_lane_cancel() {
    local pid attempt remaining
    for pid in ${pids[@]+"${pids[@]}"}; do kill -TERM -- "-$pid" 2>/dev/null || true; done
    for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
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

_lane_remove() (
    cd "${worker_dirs[$1]}" || exit 1
    _lane_identity "$1" || exit 1
    [ "$(_lane_inode .)" = "${worker_inodes[$1]}" ] || exit 1
    _lane_git worktree remove --force .
)

_lane_cleanup() {
    local status="$?" id suite unsafe=0
    trap '' INT TERM HUP
    if [ "$WORKERS_JOINED" = 0 ]; then _lane_cancel; fi
    for id in $(seq 1 "$CREATED"); do
        if [ -e "$RECDIR/.suite-$id.pid" ]; then
            suite=""
            read -r suite < "$RECDIR/.suite-$id.pid" || true
            case "$suite" in
                ''|0|*[!0-9]*) unsafe=1; status=1 ;;
                *)
                    if _lane_stop_group "$suite"; then
                        rm -f "$RECDIR/.suite-$id.pid"
                    else
                        unsafe=1; status=1
                    fi ;;
            esac
            if [ -e "$RECDIR/.suite-$id.pid" ]; then
                echo "parallel-suite-lane: worker $id descendants unconfirmed; retaining worker files" >&2
                continue
            fi
        fi
        if _lane_identity "$id"; then
            _lane_remove "$id" 2>/dev/null || { unsafe=1; status=1; }
        else
            echo "parallel-suite-lane: unsafe worker $id identity; retaining worker files" >&2
            unsafe=1; status=1
        fi
    done
    if [ "$unsafe" = 0 ]; then rmdir "$WTDIR" 2>/dev/null || status=1; fi
    rmdir "$RECDIR"/.claim-* 2>/dev/null || true
    trap - EXIT
    exit "$status"
}
trap _lane_cleanup EXIT
trap '_lane_signal 130' INT
trap '_lane_signal 143' TERM
trap '_lane_signal 129' HUP
for _w in $(seq 1 "$WORKERS"); do
    _lane_git worktree add --detach -q "$WTDIR/$_w" "$REVISION" || exit 1
    CREATED="$_w"
    worker_dirs[$_w]="$WTDIR/$_w"
    worker_gitdirs[$_w]="$(git -C "$WTDIR/$_w" rev-parse --absolute-git-dir)" || exit 1
    worker_inodes[$_w]="$(_lane_inode "$WTDIR/$_w")" || exit 1
    worker_admin_inodes[$_w]="$(_lane_inode "${worker_gitdirs[$_w]}")" || exit 1
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
_lane_reset() (
    local worker="${worker_dirs[$1]}" admin="${worker_gitdirs[$1]}"
    # Relative worktree arguments stay on this directory if its pathname moves.
    cd "$worker" || return 1
    _lane_identity "$1" || return 1
    [ "$(_lane_inode .)" = "${worker_inodes[$1]}" ] || return 1
    # Detach before restoring the pinned commit: a suite may have selected a
    # shared branch, which recovery must not move.
    GIT_COMMON_DIR="$LANE_COMMON_DIR" GIT_INDEX_FILE="$admin/index" \
        git --git-dir="$admin" --work-tree=. -c core.worktree=. \
        -c core.hooksPath=/dev/null -c core.fsmonitor=false -c submodule.recurse=false \
        checkout -q --detach --force "$REVISION" || return 1
    # The node_modules link is a symlink, which `node_modules/` in .gitignore does
    # not cover (that pattern matches directories only), so exclude it by name.
    _lane_identity "$1" || return 1
    GIT_COMMON_DIR="$LANE_COMMON_DIR" GIT_INDEX_FILE="$admin/index" \
        git --git-dir="$admin" --work-tree=. -c core.worktree=. \
        -c core.fsmonitor=false clean -fdq -e node_modules || return 1
    _lane_identity "$1"
)

_lane_worker_cleanup() {
    local status="$?"
    trap '' INT TERM HUP
    if [ -n "$suite_pid" ]; then
        if _lane_stop_group "$suite_pid"; then
            rm -f "$RECDIR/.suite-$_w.pid"
        else
            status=1
        fi
        wait "$suite_pid" 2>/dev/null || true
    else
        rm -f "$RECDIR/.suite-$_w.pid"
    fi
    rm -f "$RECDIR/.suite-$_w.out"
    trap - EXIT
    exit "$status"
}

# Job control gives workers and suites separate owned process groups.
set -m
for _w in $(seq 1 "$WORKERS"); do
    SPAWNING=1
    (
        set +m
        trap - EXIT INT TERM HUP
        SPAWNING=0; SIGNAL_STATUS=0; suite_pid=""
        trap _lane_worker_cleanup EXIT
        trap '_lane_signal 130' INT
        trap '_lane_signal 143' TERM
        trap '_lane_signal 129' HUP
        for idx in $(seq 1 "$N"); do
            # mkdir is the atomic claim: exactly one worker creates it, the rest skip.
            mkdir "$RECDIR/.claim-$idx" 2>/dev/null || continue
            f="$(sed -n "${idx}p" "$FILES")"
            rec="$RECDIR/$idx"
            _t0=$SECONDS
            set -m
            SPAWNING=1
            : > "$RECDIR/.suite-$_w.pid"
            (trap - EXIT INT TERM HUP; set +m; cd "$WTDIR/$_w" && "$@" "$f" < /dev/null) > "$RECDIR/.suite-$_w.out" 2>&1 &
            suite_pid=$!
            printf '%s\n' "$suite_pid" > "$RECDIR/.suite-$_w.pid"
            _lane_spawn_done
            set +m
            wait "$suite_pid" && rc=0 || rc=$?
            printf "%s\n" "$rc" > "$rec.rc"
            printf "%s\n" "$(( SECONDS - _t0 ))" > "$rec.time"
            _lane_stop_group "$suite_pid" && stopped=0 || stopped=1
            out="$(cat "$RECDIR/.suite-$_w.out")"
            printf "%s" "$out" > "$rec.out"
            [ "$stopped" = 0 ] || exit 1
            rm -f "$RECDIR/.suite-$_w.pid"
            suite_pid=""
            if ! _lane_reset "$_w"; then
                echo "parallel-suite-lane: cannot restore worker $_w after suite $idx; stopping worker" >&2
                exit 1
            fi
        done
    ) &
    pids+=("$!")
    _lane_spawn_done
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

#!/usr/bin/env bash
# Every suite starts at the scheduler's input commit, even after a predecessor
# changes HEAD. Drive the production scheduler in disposable Git repositories.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="$here/scripts/parallel-suite-lane.sh"
T="$(mktemp -d "${TMPDIR:-/tmp}/lane-head-XXXXXX")"
trap 'rm -rf "$T"' EXIT
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir "$T/repo"
git -C "$T/repo" init -q
git -C "$T/repo" config user.name Fixture
git -C "$T/repo" config user.email fixture@example.com
git -C "$T/repo" config core.hooksPath /dev/null
git -C "$T/repo" config commit.gpgSign false
printf 'older\n' > "$T/repo/tracked"
git -C "$T/repo" add tracked
git -C "$T/repo" commit -qm older
git -C "$T/repo" branch saved
saved="$(git -C "$T/repo" rev-parse saved)"
printf 'baseline\n' > "$T/repo/tracked"
git -C "$T/repo" commit -qam baseline
export LANE_EXPECTED_OID="$(git -C "$T/repo" rev-parse HEAD)"

cat > "$T/observe.sh" <<'EOF'
set -e
printf 'observed='; cat tracked
[ "$(cat tracked)" = baseline ]
[ "$(git rev-parse HEAD)" = "$LANE_EXPECTED_OID" ]
if git symbolic-ref -q HEAD; then exit 1; fi
echo restored-input-commit
EOF
cat > "$T/commit.sh" <<'EOF'
set -e
printf 'mutated\n' > tracked
git add tracked
git commit -qm mutated
echo committed-mutation
EOF
cat > "$T/rewind.sh" <<'EOF'
set -e
git checkout -q --detach HEAD^
[ "$(cat tracked)" = older ]
echo checked-out-parent
EOF
cat > "$T/branch.sh" <<'EOF'
set -e
git checkout -q saved
[ "$(cat tracked)" = older ]
echo selected-shared-branch
EOF

failed=0
for mode in commit rewind branch; do
    mkdir "$T/$mode"
    printf '%s\n' "$T/$mode.sh" "$T/observe.sh" > "$T/$mode/files"
    rc=0
    (cd "$T/repo" && bash "$LANE" 1 "$T/$mode/files" "$T/$mode" bash) > "$T/$mode/log" 2>&1 || rc=$?
    if [ "$rc" = 0 ] && [ "$(cat "$T/$mode/1.rc")" = 0 ] &&
        [ "$(cat "$T/$mode/2.rc")" = 0 ] && grep -qx restored-input-commit "$T/$mode/2.out"; then
        echo "PASS: $mode restores the detached input commit"
    else
        echo "FAIL: $mode did not restore the input commit"
        cat "$T/$mode/log" "$T/$mode/1.out" "$T/$mode/2.out"
        printf '\n'
        failed=1
    fi
done

if [ "$(git -C "$T/repo" rev-parse saved)" = "$saved" ] &&
    [ "$(git -C "$T/repo" rev-parse HEAD)" = "$LANE_EXPECTED_OID" ] &&
    [ "$(cat "$T/repo/tracked")" = baseline ]; then
    echo 'PASS: caller and suite-selected branch are unchanged'
else
    echo 'FAIL: recovery rewrote the caller or suite-selected branch'
    failed=1
fi

cat > "$T/lock.sh" <<'EOF'
set -e
printf 'mutated\n' > tracked
: > "$(git rev-parse --git-path index.lock)"
echo locked-index
EOF
mkdir "$T/locked"
printf '%s\n' "$T/lock.sh" "$T/observe.sh" > "$T/locked/files"
rc=0
(cd "$T/repo" && bash "$LANE" 1 "$T/locked/files" "$T/locked" bash) > "$T/locked/log" 2>&1 || rc=$?
if [ "$rc" -ne 0 ] && [ "$(cat "$T/locked/1.rc")" = 0 ] &&
    grep -qx locked-index "$T/locked/1.out" && [ ! -e "$T/locked/2.rc" ] &&
    grep -q 'cannot restore worker' "$T/locked/log"; then
    echo 'PASS: failed restore stops the worker and fails the scheduler'
else
    echo 'FAIL: failed restore reused the worker or returned success'
    cat "$T/locked/log"
    failed=1
fi

mkdir "$T/multi"
printf '%s\n' "$T/lock.sh" "$T/observe.sh" "$T/observe.sh" > "$T/multi/files"
rc=0
(cd "$T/repo" && bash "$LANE" 2 "$T/multi/files" "$T/multi" bash) > "$T/multi/log" 2>&1 || rc=$?
if [ "$rc" -ne 0 ] && [ "$(cat "$T/multi/1.rc")" = 0 ] &&
    [ "$(cat "$T/multi/2.rc")" = 0 ] && [ "$(cat "$T/multi/3.rc")" = 0 ] &&
    grep -q 'cannot restore worker' "$T/multi/log"; then
    echo 'PASS: healthy worker finishes while scheduler retains the restore failure'
else
    echo 'FAIL: multi-worker run lost the restore failure or remaining suites'
    cat "$T/multi/log"
    failed=1
fi
claims="$(find "$T/locked" "$T/multi" -maxdepth 1 -name '.claim-*')"
if [ "$(git -C "$T/repo" worktree list --porcelain | sed -n '/^worktree /p' | wc -l | tr -d ' ')" = 1 ] &&
    [ -z "$claims" ]; then
    echo 'PASS: failed workers leave no registered worktrees or claim markers'
else
    echo 'FAIL: failed worker cleanup left scheduler resources'
    failed=1
fi

cat > "$T/git-env.sh" <<'EOF'
set -e
[ "$(git rev-parse --show-toplevel)" = "$(pwd -P)" ]
printf 'suite-edit\n' > tracked
git add tracked
echo edited-lane-index
EOF
printf 'caller-staged\n' > "$T/repo/tracked"
git -C "$T/repo" add tracked
cp "$T/repo/.git/index" "$T/caller.index"
printf 'caller-unstaged\n' > "$T/repo/tracked"
printf 'keep\n' > "$T/repo/untracked"
mkdir "$T/git-env"
printf '%s\n' "$T/git-env.sh" "$T/observe.sh" > "$T/git-env/files"
rc=0
(cd "$T" && GIT_DIR="$T/repo/.git" GIT_WORK_TREE="$T/repo" GIT_INDEX_FILE="$T/repo/.git/index" \
    bash "$LANE" 1 "$T/git-env/files" "$T/git-env" bash) > "$T/git-env/log" 2>&1 || rc=$?
if [ "$rc" = 0 ] && [ "$(cat "$T/git-env/1.rc")" = 0 ] &&
    [ "$(cat "$T/git-env/2.rc")" = 0 ] && cmp -s "$T/caller.index" "$T/repo/.git/index" &&
    [ "$(cat "$T/repo/tracked")" = caller-unstaged ] && [ "$(cat "$T/repo/untracked")" = keep ]; then
    echo 'PASS: inherited Git selectors cannot change caller staged or unstaged work'
else
    echo 'FAIL: inherited Git selectors reached the caller or prevented suite isolation'
    cat "$T/git-env/log"
    failed=1
fi
exit "$failed"

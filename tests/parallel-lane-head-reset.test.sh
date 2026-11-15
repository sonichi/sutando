#!/usr/bin/env bash
# Every suite starts at the scheduler's input commit, even after a predecessor
# changes HEAD. Drive the production scheduler in disposable Git repositories.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="$here/scripts/parallel-suite-lane.sh"
T="$(mktemp -d "${TMPDIR:-/tmp}/lane-head-XXXXXX")"
trap 'rm -rf "$T"' EXIT
mkdir "$T/tmp"
export TMPDIR="$T/tmp"
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir "$T/repo"
git -C "$T/repo" init -q
git -C "$T/repo" config user.name Fixture
git -C "$T/repo" config user.email fixture@example.com
git -C "$T/repo" config core.hooksPath /dev/null
git -C "$T/repo" config commit.gpgSign false
printf 'older\n' > "$T/repo/tracked"
printf '.coverage.*\nnode_modules/\n' > "$T/repo/.gitignore"
git -C "$T/repo" add tracked .gitignore
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
cat > "$T/redirect.sh" <<'EOF'
set -e
worker="$(pwd -P)"
admin="$(git rev-parse --absolute-git-dir)"
case "$LANE_REDIRECT" in
    gitfile) printf 'gitdir: %s/.git\n' "$LANE_CALLER" > .git ;;
    symlink) mv "$worker" "$worker.saved"; ln -s "$LANE_CALLER" "$worker" ;;
    replacement)
        mv "$worker" "$worker.saved"
        mkdir "$worker"
        cp "$worker.saved/.git" "$worker/.git"
        printf 'retain-replacement\n' > "$worker/replacement-marker"
        printf '%s\n' "$worker" > "$LANE_REPLACEMENT_RECORD"
        ;;
    config) git config --local core.worktree "$LANE_CALLER" ;;
    commondir) printf '%s/.git\n' "$LANE_OTHER" > "$admin/commondir" ;;
    index) rm "$admin/index"; ln -s "$LANE_CALLER/.git/index" "$admin/index" ;;
    backlink) printf '%s/.git\n' "$LANE_CALLER" > "$admin/gitdir" ;;
    admin) mv "$admin" "$admin.saved"; ln -s "$LANE_CALLER/.git" "$admin" ;;
esac
echo redirected-worker
EOF
git clone -q --no-local "$T/repo" "$T/other"
export LANE_OTHER="$T/other"
for mode in gitfile symlink replacement config commondir index backlink admin; do
    caller="$T/$mode-repo"
    git clone -q --no-local "$T/repo" "$caller"
    caller_head="$(git -C "$caller" symbolic-ref HEAD)"
    caller_oid="$(git -C "$caller" rev-parse HEAD)"
    printf 'caller-staged\n' > "$caller/tracked"
    git -C "$caller" add tracked
    cp "$caller/.git/index" "$T/$mode.index"
    printf 'caller-unstaged\n' > "$caller/tracked"
    printf 'keep\n' > "$caller/untracked"
    mkdir "$T/$mode"
    printf '%s\n' "$T/redirect.sh" "$T/observe.sh" > "$T/$mode/files"
    rc=0
    (cd "$caller" && LANE_REDIRECT="$mode" LANE_CALLER="$caller" LANE_REPLACEMENT_RECORD="$T/replacement-path" \
        bash "$LANE" 1 "$T/$mode/files" "$T/$mode" bash) > "$T/$mode/log" 2>&1 || rc=$?
    index_same=false
    cmp -s "$T/$mode.index" "$caller/.git/index" && index_same=true
    staged="$(git --git-dir="$caller/.git" show :tracked)"
    working="$(cat "$caller/tracked")"
    untracked=false
    [ -f "$caller/untracked" ] && [ "$(cat "$caller/untracked")" = keep ] && untracked=true
    head_same=false
    if [ "$(git --git-dir="$caller/.git" symbolic-ref -q HEAD)" = "$caller_head" ] &&
        [ "$(git --git-dir="$caller/.git" rev-parse HEAD)" = "$caller_oid" ]; then head_same=true; fi
    retained=true
    if [ "$mode" = replacement ]; then
        replacement="$(cat "$T/replacement-path")"
        [ -f "$replacement/replacement-marker" ] &&
            [ "$(cat "$replacement/replacement-marker")" = retain-replacement ] || retained=false
    fi
    if [ "$rc" -ne 0 ] && [ "$(cat "$T/$mode/1.rc")" = 0 ] &&
        [ ! -e "$T/$mode/2.rc" ] && [ "$index_same" = true ] &&
        [ "$staged" = caller-staged ] && [ "$working" = caller-unstaged ] &&
        [ "$untracked" = true ] && [ "$head_same" = true ] && [ "$retained" = true ]; then
        echo "PASS: $mode redirection fails without changing caller index bytes or local work"
    else
        echo "FAIL: $mode rc=$rc index_unchanged=$index_same staged=$staged working=$working untracked=$untracked head_unchanged=$head_same retained=$retained"
        cat "$T/$mode/log"
        failed=1
    fi
done

cat > "$T/suite-failure.sh" <<'EOF'
printf 'coverage-fragment\n' > .coverage.lane-probe
echo suite-failed
exit 7
EOF
mkdir "$T/suite-failure"
printf '%s\n' "$T/suite-failure.sh" "$T/observe.sh" > "$T/suite-failure/files"
rc=0
(cd "$T/repo" && bash "$LANE" 1 "$T/suite-failure/files" "$T/suite-failure" bash) > "$T/suite-failure/log" 2>&1 || rc=$?
if [ "$rc" = 0 ] && [ "$(cat "$T/suite-failure/1.rc")" = 7 ] &&
    [ "$(cat "$T/suite-failure/2.rc")" = 0 ] &&
    [ "$(cat "$T/repo/.coverage.lane-probe")" = coverage-fragment ]; then
    echo 'PASS: suite failure stays in its record, next suite runs, and coverage comes home'
else
    echo 'FAIL: suite failure changed scheduler status, blocked the queue, or lost coverage'
    cat "$T/suite-failure/log"
    failed=1
fi

cat > "$T/interrupt.py" <<'EOF'
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

rec = Path(os.environ["LANE_INTERRUPT_RECDIR"])
ready, late = rec / "ready", rec / "late"
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
watcher = """import sys, time
from pathlib import Path
root, marker = map(Path, sys.argv[1:])
for _ in range(400):
    if not root.exists():
        marker.write_text("cleanup reached a running descendant\\n")
        break
    time.sleep(0.01)
"""
child = subprocess.Popen([sys.executable, "-c", watcher, str(Path.cwd()), str(rec / "cleanup-live")])
ready.write_text(f"{os.getpid()} {child.pid} {Path.cwd()}\n")
child.wait()
late.write_text("suite continued after interruption\n")
EOF
mkdir "$T/interrupt"
printf '%s\n' "$T/interrupt.py" > "$T/interrupt/files"
rc=0
python3 - "$LANE" "$T/repo" "$T/interrupt" <<'EOF' || rc=$?
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

lane, repo, rec = sys.argv[1:]
rec = Path(rec)
ready, late = rec / "ready", rec / "late"
index = Path(repo, ".git/index").read_bytes()
with (rec / "log").open("w") as log:
    scheduler = subprocess.Popen(["bash", lane, "1", str(rec / "files"), str(rec),
                                  sys.executable], cwd=repo, stdout=log, stderr=log,
                                 start_new_session=True,
                                 env=dict(os.environ, LANE_INTERRUPT_RECDIR=str(rec)))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert scheduler.poll() is None, "scheduler exited before suite started"
            assert time.monotonic() < deadline, "suite did not become ready"
            time.sleep(0.02)
        suite, child, worker = ready.read_text().split()
        os.killpg(scheduler.pid, signal.SIGINT)
        # Allow worker cancellation, descendant escalation and final cleanup.
        status = scheduler.wait(timeout=15)
        def running(pid):
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                return False
            stat = Path("/proc", pid, "stat")
            if not Path("/proc").exists():
                return True
            try:
                return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                return False
        assert status != 0, f"interrupt returned {status}"
        deadline = time.monotonic() + 1
        while (running(suite) or running(child)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not running(suite) and not running(child), "suite descendants survived scheduler exit"
        assert not late.exists(), "suite continued after interruption"
        assert not (rec / "cleanup-live").exists(), "cleanup reached a running descendant"
        assert not Path(worker).exists(), "worker was not cleaned up"
        assert not list(rec.glob(".claim-*")), "interrupt left queue claims"
        assert Path(repo, ".git/index").read_bytes() == index, "caller index changed"
        assert Path(repo, "tracked").read_text() == "caller-unstaged\n", "caller work changed"
        assert Path(repo, "untracked").read_text() == "keep\n", "caller untracked file changed"
    finally:
        if scheduler.poll() is None:
            os.killpg(scheduler.pid, signal.SIGKILL)
            scheduler.wait()
        if ready.exists():
            for pid in ready.read_text().split()[:2]:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
EOF
if [ "$rc" = 0 ]; then
    echo 'PASS: SIGINT terminates suite descendants before cleanup and preserves caller work'
else
    echo 'FAIL: SIGINT left running suites, scheduler resources, or changed caller work'
    cat "$T/interrupt/log"
    failed=1
fi
exit "$failed"

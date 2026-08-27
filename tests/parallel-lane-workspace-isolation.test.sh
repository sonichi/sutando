#!/usr/bin/env bash
# No two suites may share a resolved workspace or a scheduler slot. Every
# check drives the SHIPPED scheduler (parallel-suite-lane.sh), not a copy.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="$here/scripts/parallel-suite-lane.sh"
A="tests/discord-bridge-reply-directive.test.py"
B="tests/dm-result-adoption-gap.test.py"
fail=0
TRIALS="${ISOLATION_TRIALS:-4}"

# --- 1. Baseline control: the shared-cwd shape must FAIL, or nothing below
# means anything — the collision needs overlap and a single trial can pass.
shared=0
for ((i=0; i<TRIALS; i++)); do
    ( cd "$here" && python3 "$A" >/dev/null 2>&1 ) & pid=$!
    sleep 0.12
    ( cd "$here" && python3 "$B" >/dev/null 2>&1 )
    wait $pid || shared=$((shared+1))
done
if [ "$shared" -eq 0 ]; then
    echo "  FAIL: shared-cwd baseline passed $TRIALS/$TRIALS — control cannot detect the race"; fail=1
else
    echo "  ok   shared cwd collides ($shared/$TRIALS failed)"
fi

# --- 2. The same racing pair through the SHIPPED scheduler: with 2 workers the
# pair runs concurrently in two worktrees, and both must pass every trial.
lane_pair() {
    local rec files
    rec="$(mktemp -d)"; files="$rec/files"
    printf '%s\n%s\n' "$A" "$B" > "$files"
    ( cd "$here" && bash "$LANE" 2 "$files" "$rec" python3 )
    local r1 r2
    r1="$(cat "$rec/1.rc" 2>/dev/null || echo 9)"
    r2="$(cat "$rec/2.rc" 2>/dev/null || echo 9)"
    rm -rf "$rec"
    [ "$r1" = "0" ] && [ "$r2" = "0" ]
}
split=0
for ((i=0; i<TRIALS; i++)); do lane_pair || split=$((split+1)); done
if [ "$split" -ne 0 ]; then
    echo "  FAIL: shipped scheduler still collided ($split/$TRIALS)"; fail=1
else
    echo "  ok   shipped scheduler isolates the pair (0/$TRIALS)"
fi

# --- 3. Slot exclusivity: item 1 slow, 2 workers — the old round-robin
# printed OVERLAP idx=3 slot=1 on exactly this timing.
probe="$(mktemp -d)"
cat > "$probe/suite.sh" <<'EOF'
#!/usr/bin/env bash
if ! mkdir .lane-lock 2>/dev/null; then echo "OVERLAP in $PWD"; exit 1; fi
case "$1" in *one*) sleep 1.2;; *) sleep 0.2;; esac
rmdir .lane-lock
EOF
chmod +x "$probe/suite.sh"
rec="$(mktemp -d)"
printf 'one\ntwo\nthree\n' > "$rec/files"
( cd "$here" && bash "$LANE" 2 "$rec/files" "$rec" bash "$probe/suite.sh" )
if grep -q "OVERLAP" "$rec"/*.out 2>/dev/null; then
    echo "  FAIL: two suites occupied one worktree:"; grep "OVERLAP" "$rec"/*.out; fail=1
else
    echo "  ok   no slot ever held two suites (3 items, 2 workers, slow head)"
fi
rm -rf "$rec" "$probe"

# --- 4. Fragment collection: a worktree-cwd fragment must land in the caller's
# cwd, where `coverage combine` searches (end-to-end proof: CI's own gate job).
probe2="$(mktemp -d)"
printf '#!/usr/bin/env bash\ndate > .coverage.frag-probe\n' > "$probe2/writer.sh"
chmod +x "$probe2/writer.sh"
rec="$(mktemp -d)"; caller="$(mktemp -d)"
printf 'x\n' > "$rec/files"
git -C "$here" worktree list >/dev/null 2>&1 || { echo "  FAIL: not a git checkout"; exit 1; }
( cd "$caller" && GIT_DIR="$here/.git" GIT_WORK_TREE="$here" bash "$LANE" 1 "$rec/files" "$rec" bash "$probe2/writer.sh" ) >/dev/null 2>&1
if ls "$caller"/.coverage.frag-probe >/dev/null 2>&1; then
    echo "  ok   fragment came home to the caller's cwd"
else
    echo "  FAIL: fragment marooned — nothing for coverage combine to find"; fail=1
fi
rm -rf "$rec" "$probe2" "$caller"

[ "$fail" -eq 0 ] && echo "Parallel-lane workspace isolation holds."
exit "$fail"

#!/usr/bin/env bash
# A worker's worktree is reset between suites: what one suite leaves behind —
# an untracked file, an edit to a tracked file — the next suite on that worker
# never sees. One worker, two suites, so the pair is guaranteed to share a tree.
# Drives the SHIPPED scheduler, not a copy.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="$here/scripts/parallel-suite-lane.sh"
T="$(mktemp -d "${TMPDIR:-/tmp}/lane-reset-XXXXXX")"
trap 'rm -rf "$T"' EXIT
fail=0

# Suite 1 dirties its worktree both ways and says so; suite 2 reports what it finds.
cat > "$T/s1.sh" <<'EOF'
printf 'leftover\n' > "$PWD/lane-reset-leftover.txt"
printf '\n# dirtied by a lane suite\n' >> "$PWD/README.md"
printf '\n# staged by a lane suite\n' >> "$PWD/CONTRIBUTING.md"; git add CONTRIBUTING.md
[ -e "$PWD/lane-reset-leftover.txt" ] && echo "wrote-untracked"
git diff --quiet -- README.md || echo "edited-tracked"
git diff --cached --quiet -- CONTRIBUTING.md || echo "staged-tracked"
exit 0
EOF
cat > "$T/s2.sh" <<'EOF'
[ -e "$PWD/lane-reset-leftover.txt" ] && echo "untracked-leftover-present"
git diff --quiet -- README.md || echo "tracked-edit-present"
git diff --cached --quiet -- CONTRIBUTING.md || echo "staged-edit-present"
git diff --quiet HEAD -- CONTRIBUTING.md || echo "staged-edit-in-tree"
[ -L "$PWD/node_modules" ] && echo "node-modules-link-present"
echo "checked"
exit 0
EOF
printf '%s\n%s\n' "$T/s1.sh" "$T/s2.sh" > "$T/files"
mkdir -p "$T/rec"

( cd "$here" && bash "$LANE" 1 "$T/files" "$T/rec" bash )

out2="$(cat "$T/rec/2.out" 2>/dev/null)"
case "$out2" in
    *checked*) ;;
    *) echo "  FAIL suite 2 did not run: [$out2]"; fail=1 ;;
esac
case "$out2" in
    *untracked-leftover-present*) echo "  FAIL suite 2 saw suite 1's untracked file"; fail=1 ;;
    *) echo "  ok   suite 1's untracked file was gone before suite 2" ;;
esac
case "$out2" in
    *tracked-edit-present*) echo "  FAIL suite 2 saw suite 1's edit to a tracked file"; fail=1 ;;
    *) echo "  ok   suite 1's edit to a tracked file was reverted before suite 2" ;;
esac
# A staged edit lives in the index; a checkout from the index would keep it.
case "$out2" in
    *staged-edit-present*|*staged-edit-in-tree*) echo "  FAIL suite 2 saw suite 1's STAGED edit (index not reset)"; fail=1 ;;
    *) echo "  ok   suite 1's staged edit was dropped from index and tree before suite 2" ;;
esac
# The shared node_modules link is a symlink, which .gitignore's `node_modules/`
# does not cover; the reset must leave it for the next suite.
if [ -d "$here/node_modules" ]; then
    case "$out2" in
        *node-modules-link-present*) echo "  ok   the node_modules link survived the reset" ;;
        *) echo "  FAIL the reset removed the node_modules link"; fail=1 ;;
    esac
else
    echo "  skip node_modules link check: caller has no node_modules"
fi
# Control: suite 1 really did dirty the tree, or the checks above prove nothing.
out1="$(cat "$T/rec/1.out" 2>/dev/null)"
case "$out1" in
    *wrote-untracked*edited-tracked*staged-tracked*) echo "  ok   suite 1 dirtied its tree three ways (control)" ;;
    *) echo "  FAIL suite 1 did not dirty its tree: [$out1]"; fail=1 ;;
esac
# The caller's own tree is untouched: the reset happens in the lane worktrees only.
if [ -e "$here/lane-reset-leftover.txt" ]; then
    echo "  FAIL the leftover landed in the caller's tree"; fail=1; rm -f "$here/lane-reset-leftover.txt"
else
    echo "  ok   caller's tree untouched"
fi

# A caller that exports GIT_DIR/GIT_WORK_TREE (the coverage-fragment check does)
# must keep its own dirty tree: the reset targets the lane worktree, never that one.
decoy="$(mktemp -d "${TMPDIR:-/tmp}/lane-decoy-XXXXXX")"
( cd "$decoy" && git init -q && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init \
    && printf 'tracked\n' > tracked.txt && git add tracked.txt && git -c user.email=t@t -c user.name=t commit -q -m tracked )
printf 'uncommitted work\n' > "$decoy/uncommitted.txt"
printf 'edited\n' >> "$decoy/tracked.txt"
printf 'exit 0\n' > "$T/s3.sh"
printf '%s\n' "$T/s3.sh" > "$T/files-decoy"
mkdir -p "$T/rec-decoy"
( cd "$T" && GIT_DIR="$decoy/.git" GIT_WORK_TREE="$decoy" bash "$LANE" 1 "$T/files-decoy" "$T/rec-decoy" bash ) >/dev/null 2>&1
if [ -e "$decoy/uncommitted.txt" ] && [ "$(cat "$decoy/tracked.txt")" = "$(printf 'tracked\nedited')" ]; then
    echo "  ok   a caller's exported GIT_DIR/GIT_WORK_TREE tree keeps its uncommitted work"
else
    echo "  FAIL the reset reached the caller's GIT_WORK_TREE: untracked=$([ -e "$decoy/uncommitted.txt" ] && echo kept || echo WIPED) tracked=[$(cat "$decoy/tracked.txt")]"; fail=1
fi
rm -rf "$decoy"

if [ "$fail" = 0 ]; then
    echo "parallel-lane-worktree-reset: all checks pass"
else
    echo "parallel-lane-worktree-reset: FAILED"; exit 1
fi

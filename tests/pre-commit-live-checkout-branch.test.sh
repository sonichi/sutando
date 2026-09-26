#!/usr/bin/env bash
# The primary checkout must refuse a commit on any branch but main; a real
# `git worktree` checkout (the intended place to author a feature branch)
# must be exempt. This guard exists because the same mistake -- checking out
# a feature branch directly on the primary checkout and never switching back
# -- recurred four times (2026-07-25, 08-06, 09-14, 09-26) before it was
# added; see feedback_author_prs_in_worktrees_only in core memory.
set -uo pipefail

HOOK_SRC="$(cd "$(dirname "$0")/.." && pwd)"
fails=0
ok () { printf '  ok   %s\n' "$1"; }
bad () { printf '  FAIL %s\n' "$1"; fails=$((fails + 1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- primary checkout ---
PRIMARY="$TMP/primary"
mkdir -p "$PRIMARY"
cd "$PRIMARY" || exit 1
git init -q .
git config user.email t@example.com
git config user.name t
mkdir -p .githooks workspace
cp "$HOOK_SRC/.githooks/pre-commit" .githooks/pre-commit
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
touch workspace/.gitkeep
git add -A && git commit -q --no-verify -m base
git branch -m main 2>/dev/null || true

# 1. committing on main in the primary checkout is allowed.
echo x > a.txt
git add a.txt
if git commit -q -m "on main" >/dev/null 2>&1; then ok "primary checkout on main: commit allowed"
else bad "primary checkout on main: commit allowed"; fi

# 2. committing on a feature branch in the primary checkout is refused.
git checkout -q -b feat/example
echo y > b.txt
git add b.txt
if git commit -q -m "on feature branch" >/dev/null 2>&1; then
    bad "primary checkout on feature branch: commit refused"
else
    ok "primary checkout on feature branch: commit refused"
fi
git reset -q HEAD -- . 2>/dev/null; rm -f b.txt

# 3. the escape hatch still works.
echo y > b.txt
git add b.txt
if git commit -q --no-verify -m "on feature branch, --no-verify" >/dev/null 2>&1; then
    ok "primary checkout on feature branch: --no-verify escape hatch works"
else
    bad "primary checkout on feature branch: --no-verify escape hatch works"
fi
git checkout -q main

# 4. detached HEAD (e.g. `git worktree add --detach` predecessor state) is
#    exempt -- it is a read, not an authored branch.
detached_sha="$(git rev-parse HEAD)"
git checkout -q "$detached_sha"
echo z > c.txt
git add c.txt
if git commit -q -m "detached HEAD" >/dev/null 2>&1; then ok "primary checkout, detached HEAD: commit allowed"
else bad "primary checkout, detached HEAD: commit allowed"; fi
git checkout -q main
rm -f c.txt

# --- real worktree ---
WT="$TMP/wt-example"
git -C "$PRIMARY" worktree add -q -b feat/from-worktree "$WT" main
cd "$WT" || exit 1
echo w > d.txt
git add d.txt
if git commit -q -m "in a real worktree" >/dev/null 2>&1; then
    ok "real worktree on a feature branch: commit allowed"
else
    bad "real worktree on a feature branch: commit allowed"
fi

if [ "$fails" -eq 0 ]; then echo "pre-commit-live-checkout-branch: all checks passed"; else echo "FAILED: $fails"; fi
exit $(( fails > 0 ? 1 : 0 ))

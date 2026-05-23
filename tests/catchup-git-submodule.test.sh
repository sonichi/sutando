#!/usr/bin/env bash
# Tests for PR #1076: catchup-after-startup submodule/worktree .git detection fix
# and git log | head SIGPIPE de-flake.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/skills/catchup-after-startup/scripts/catchup-after-startup.sh"

pass=0; fail=0
ok()   { echo "  ✓ $1"; pass=$((pass + 1)); }
fail() { echo "  ✗ $1"; fail=$((fail + 1)); }

echo "catchup-after-startup submodule/git-section fix tests (#1076)"

[ -f "$SCRIPT" ] && ok "catchup-after-startup.sh exists" || { fail "script missing"; exit 1; }

# ── Static content checks ────────────────────────────────────────────────────

# Auto-detect probe must use -e (not -d) for .git
grep -q '"\$_cand/.git"' "$SCRIPT" || { fail ".git check not found in auto-detect"; exit 1; }
if grep -q '\-e.*"\$_cand/\.git"\|\-e "\$_cand/\.git"' "$SCRIPT"; then
  ok "auto-detect probe uses -e for .git (handles submodule/worktree)"
else
  fail "auto-detect probe must use -e not -d for .git"
fi

if grep -q '\-d "\$_cand/\.git"' "$SCRIPT"; then
  fail "auto-detect probe still uses -d — must be -e"
else
  ok "auto-detect probe does NOT use -d for .git"
fi

# Commits section gate must use -e (not -d) for .git
if grep -q '\-e "\$REPO/\.git"' "$SCRIPT"; then
  ok "commits section gate uses -e for .git"
else
  fail "commits section gate must use -e not -d for .git"
fi

if grep -q '\-d "\$REPO/\.git"' "$SCRIPT"; then
  fail "commits section gate still uses -d — must be -e"
else
  ok "commits section gate does NOT use -d for .git"
fi

# SIGPIPE fix: git log should be captured in an `if git_rows=$(...)` block,
# NOT piped directly into head inside a command substitution.
if grep -q 'if git_rows=\$(git' "$SCRIPT"; then
  ok "git log captured in if-block (SIGPIPE fix present)"
else
  fail "git log should be captured in if-block to avoid SIGPIPE"
fi

# head should operate on the captured variable, not inline in the git pipe
if grep -q 'printf.*git_rows.*head -25\|printf.*\$git_rows.*| head\|printf.*git_rows.*| head' "$SCRIPT"; then
  ok "head -25 operates on captured string (not live git pipe)"
else
  fail "head -25 should operate on captured string, not live git pipe"
fi

# The old brittle pattern (pipe | head inside $(...)) must be gone
if grep -q "git.*log.*| head -25" "$SCRIPT"; then
  fail "old brittle git log | head -25 pipe still present"
else
  ok "old git log | head -25 pipe removed"
fi

# ── Integration: -e accepts .git as a file ───────────────────────────────────
echo
echo "  [.git file acceptance test]"
TMP="$(mktemp -d)"
# Simulate a submodule checkout: .git is a FILE (gitdir pointer), not a dir
echo "gitdir: ../../.git/modules/sutando" > "$TMP/.git"
mkdir -p "$TMP/skills"
touch "$TMP/CLAUDE.md"

# The -e test should accept it; the -d test should reject it
if [ -e "$TMP/.git" ]; then
  ok "-e accepts .git as a file (submodule checkout)"
else
  fail "-e rejected .git file — unexpected"
fi

if [ -d "$TMP/.git" ]; then
  fail "-d would (incorrectly) accept .git as a regular directory here"
else
  ok "-d correctly rejects .git-as-file (confirms -d was the bug)"
fi

rm -rf "$TMP"

echo
echo "Results: $pass passed, $fail failed"
[ $fail -eq 0 ] && echo "All $pass catchup-git-submodule tests passed." && exit 0 || exit 1

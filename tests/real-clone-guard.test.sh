#!/usr/bin/env bash
# Regression for tests/lib/real-clone-guard.sh — the tripwire that proves
# sync-workspace.test.sh did not write into a real clone.
#
# Why this file exists (john-the-dev, #2440): the guard originally compared only
# `git rev-parse HEAD`. A reached clone can be left dirty with HEAD unchanged —
# untracked probe file, staged-but-uncommitted write, or a commit that failed after
# `git add` — and the guard returned success. An untracked file in the operator's
# clone is carried by the next legitimate sync: the two-hop leak the suite exists to
# stop. Case 2 and case 3 below are the discriminating cases: they FAIL against the
# HEAD-only guard and PASS against the current one.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/lib/real-clone-guard.sh"

pass=0; fail=0
check() { # <name> <expected 0|1> <actual>
  if [ "$2" = "$3" ]; then echo "  ok  $1"; pass=$((pass+1));
  else echo "  FAIL $1 (expected rc=$2, got rc=$3)"; fail=$((fail+1)); fi
}
mkfixture() {
  local d; d="$(mktemp -d -t rcg-fixture.XXXXXX)"
  git -C "$d" init -q
  git -C "$d" config user.email t@invalid; git -C "$d" config user.name t
  echo seed > "$d/seed.txt"; git -C "$d" add seed.txt
  git -C "$d" commit -qm seed
  printf '%s' "$d"
}

# 1. untouched clone -> guard passes
D="$(mkfixture)"; rcg_snapshot "$D"; rcg_assert >/dev/null 2>&1
check "untouched clone passes" 0 $?
rm -rf "$D"

# 2. DISCRIMINATING: untracked write, HEAD unchanged -> guard must FAIL
D="$(mkfixture)"; rcg_snapshot "$D"
H_BEFORE="$(git -C "$D" rev-parse HEAD)"
touch "$D/.probe-untracked"
out="$(rcg_assert 2>&1)"; rc=$?
check "untracked write (same HEAD) fails" 1 $rc
[ "$(git -C "$D" rev-parse HEAD)" = "$H_BEFORE" ] \
  && { echo "  ok  ...and HEAD really was unchanged (HEAD-only guard would pass)"; pass=$((pass+1)); } \
  || { echo "  FAIL HEAD moved — not a discriminating case"; fail=$((fail+1)); }
case "$out" in *".probe-untracked"*) echo "  ok  ...names the offending entry"; pass=$((pass+1));;
  *) echo "  FAIL did not name the entry"; fail=$((fail+1));; esac
rm -rf "$D"

# 3. DISCRIMINATING: staged write, HEAD unchanged -> guard must FAIL
D="$(mkfixture)"; rcg_snapshot "$D"
H_BEFORE="$(git -C "$D" rev-parse HEAD)"
echo p > "$D/.probe-staged"; git -C "$D" add .probe-staged
rcg_assert >/dev/null 2>&1
check "staged write (same HEAD) fails" 1 $?
[ "$(git -C "$D" rev-parse HEAD)" = "$H_BEFORE" ] \
  && { echo "  ok  ...and HEAD really was unchanged"; pass=$((pass+1)); } \
  || { echo "  FAIL HEAD moved"; fail=$((fail+1)); }
rm -rf "$D"

# 4. a commit still fails (the original HEAD signal is not lost)
D="$(mkfixture)"; rcg_snapshot "$D"
echo more >> "$D/seed.txt"; git -C "$D" add seed.txt; git -C "$D" commit -qm probe
rcg_assert >/dev/null 2>&1
check "commit fails (HEAD signal preserved)" 1 $?
rm -rf "$D"

# 5. a clone ALREADY dirty before the snapshot is not a false positive —
#    the guard compares before-vs-after, it does not demand cleanliness.
D="$(mkfixture)"; touch "$D/pre-existing-untracked"
rcg_snapshot "$D"; rcg_assert >/dev/null 2>&1
check "pre-existing dirt is not a false positive" 0 $?
rm -rf "$D"

# 6. no .git -> guard is inert rather than erroring
D="$(mktemp -d -t rcg-nogit.XXXXXX)"; rcg_snapshot "$D"; rcg_assert >/dev/null 2>&1
check "non-repo path is inert" 0 $?
rm -rf "$D"

# --- Review 2 (qingyun, #2440): status codes are not content -------------------
# `git status --porcelain` prints " M path" for an already-modified tracked file
# both before and after it is overwritten, and "?? path" for an existing untracked
# file either way. So a write that CLOBBERS the operator's own uncommitted work left
# the status output byte-identical and the guard returned success. These two cases
# FAIL against a status-only guard and pass against the content-digest one.

# 7. DISCRIMINATING: overwrite an ALREADY-MODIFIED tracked file (status unchanged)
D="$(mkfixture)"
echo "operator work" > "$D/seed.txt"          # dirty BEFORE the snapshot
rcg_snapshot "$D"
S_BEFORE="$(git -C "$D" status --porcelain -uall)"
echo "clobbered by the suite" > "$D/seed.txt"  # same status code, different bytes
S_AFTER="$(git -C "$D" status --porcelain -uall)"
out="$(rcg_assert 2>&1)"; rc=$?
check "overwriting an already-MODIFIED tracked file fails" 1 $rc
[ "$S_BEFORE" = "$S_AFTER" ] \
  && { echo "  ok  ...and porcelain status was IDENTICAL (status-only guard would pass)"; pass=$((pass+1)); } \
  || { echo "  FAIL status changed — not a discriminating case"; fail=$((fail+1)); }
case "$out" in *"CONTENT changed"*) echo "  ok  ...and the message names the clobber case"; pass=$((pass+1));;
  *) echo "  FAIL clobber case not explained"; fail=$((fail+1));; esac
rm -rf "$D"

# 8. DISCRIMINATING: overwrite an EXISTING untracked file (status unchanged)
D="$(mkfixture)"
echo "operator scratch" > "$D/scratch.txt"     # untracked BEFORE the snapshot
rcg_snapshot "$D"
S_BEFORE="$(git -C "$D" status --porcelain -uall)"
echo "clobbered" > "$D/scratch.txt"
S_AFTER="$(git -C "$D" status --porcelain -uall)"
rcg_assert >/dev/null 2>&1
check "overwriting an existing UNTRACKED file fails" 1 $?
[ "$S_BEFORE" = "$S_AFTER" ] \
  && { echo "  ok  ...and porcelain status was IDENTICAL"; pass=$((pass+1)); } \
  || { echo "  FAIL status changed"; fail=$((fail+1)); }
rm -rf "$D"

# 9. an untouched already-dirty clone is STILL not a false positive
D="$(mkfixture)"
echo "operator work" > "$D/seed.txt"; touch "$D/scratch.txt"
rcg_snapshot "$D"; rcg_assert >/dev/null 2>&1
check "already-dirty clone left alone is not a false positive" 0 $?
rm -rf "$D"

# --- Shape-hunt round (self-audit, not review) ---------------------------------
# After HEAD-only and status-code-only, I went looking for the same SHAPE — a proxy
# for "the clone changed" rather than the change itself — instead of waiting for the
# next review. Three hypotheses, all three hit. Two are fixed below; the third is a
# named limitation, pinned here so it cannot be quietly forgotten.

# 10. DISCRIMINATING: a hook dropped into .git/hooks (arbitrary code on next commit)
D="$(mkfixture)"; rcg_snapshot "$D"
S_BEFORE="$(git -C "$D" status --porcelain -uall)"
printf '#!/bin/sh\necho pwned\n' > "$D/.git/hooks/post-commit"; chmod +x "$D/.git/hooks/post-commit"
rcg_assert >/dev/null 2>&1
check "a hook written into .git/hooks fails" 1 $?
[ "$S_BEFORE" = "$(git -C "$D" status --porcelain -uall)" ] \
  && { echo "  ok  ...and git status reported NOTHING (working-tree checks are blind here)"; pass=$((pass+1)); } \
  || { echo "  FAIL status changed — not a discriminating case"; fail=$((fail+1)); }
rm -rf "$D"

# 11. DISCRIMINATING: remote.origin.url repointed — redirects where a sync PUSHES
D="$(mkfixture)"; rcg_snapshot "$D"
git -C "$D" config remote.origin.url "https://elsewhere.invalid/x.git"
rcg_assert >/dev/null 2>&1
check "repointing remote.origin.url fails" 1 $?
rm -rf "$D"

# 12. ordinary git reads must NOT trip it (the false-positive that would get the
#     guard switched off — .git/ churns on index refresh, logs/HEAD, packed refs)
D="$(mkfixture)"; rcg_snapshot "$D"
git -C "$D" status >/dev/null 2>&1; git -C "$D" log -1 >/dev/null 2>&1
git -C "$D" diff >/dev/null 2>&1; git -C "$D" rev-parse HEAD >/dev/null 2>&1
rcg_assert >/dev/null 2>&1
check "read-only git commands are not a false positive" 0 $?
rm -rf "$D"

# 13. NAMED LIMITATION (documented, not a defect to be surprised by later): a write
#     to a .gitignore-matched path is NOT detected. --exclude-standard skips ignored
#     files by design, and dropping it would walk node_modules-scale trees.
D="$(mkfixture)"
echo "secrets/" > "$D/.gitignore"; git -C "$D" add .gitignore; git -C "$D" commit -qm ignore
rcg_snapshot "$D"
mkdir -p "$D/secrets"; echo leaked > "$D/secrets/probe.txt"
rcg_assert >/dev/null 2>&1
check "KNOWN GAP: a write to an ignored path is not detected" 0 $?
echo "      (asserted as 0 on purpose — pins the limitation so it stays visible)"

rm -rf "$D"

# 14. DISCRIMINATING (qingyun, round 3): the INDEX is a third surface. `git diff HEAD`
#     compares WORKTREE to HEAD and ignores staged content, so on an already-`MM` path
#     the staged blob can be swapped while status, worktree bytes and that diff are all
#     byte-identical. Fails against a guard without `git diff --cached`.
D="$(mkfixture)"
echo index-before > "$D/seed.txt"; git -C "$D" add seed.txt      # staged change
echo worktree-before > "$D/seed.txt"                              # + unstaged -> MM
rcg_snapshot "$D"
S_BEFORE="$(git -C "$D" status --porcelain -uall)"
W_BEFORE="$(cat "$D/seed.txt")"
blob=$(printf 'index-after\n' | git -C "$D" hash-object -w --stdin)
git -C "$D" update-index --cacheinfo 100644,"$blob",seed.txt      # ONLY the index moves
rcg_assert >/dev/null 2>&1
check "replacing ONLY the staged blob fails" 1 $?
[ "$S_BEFORE" = "$(git -C "$D" status --porcelain -uall)" ] \
  && { echo "  ok  ...and porcelain status was IDENTICAL (MM either way)"; pass=$((pass+1)); } \
  || { echo "  FAIL status changed — not discriminating"; fail=$((fail+1)); }
[ "$W_BEFORE" = "$(cat "$D/seed.txt")" ] \
  && { echo "  ok  ...and the WORKTREE bytes never changed"; pass=$((pass+1)); } \
  || { echo "  FAIL worktree changed — not discriminating"; fail=$((fail+1)); }
rm -rf "$D"

echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ] || exit 1

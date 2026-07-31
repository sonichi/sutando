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

echo "===================="
echo "Total: $((pass+fail)) — pass: $pass, fail: $fail"
[ "$fail" -eq 0 ] || exit 1

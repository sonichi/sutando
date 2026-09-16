#!/usr/bin/env bash
# Contract test for scripts/git-binary.sh — the shell twin of src/git_binary.py,
# restated for bash callers (src/check-pending-tasks.sh) that must not shell the
# macOS CLT stub. Same rule as tests/python-binary-sh.test.sh: NEVER execute a
# candidate to decide whether it is usable; only `xcode-select -p` is a safe probe.
#
# Run: bash tests/git-binary-sh.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()   { printf "  ok   %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  FAIL %s — %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi; }

mklab() {
  d=$(mktemp -d)
  mkdir -p "$d/bin"
  printf '#!/bin/sh\necho "STUB RAN" >> %s/stub-ran\nexit 1\n' "$d" > "$d/bin/git"
  chmod +x "$d/bin/git"
  printf '%s' "$d"
}

# --- 1. a non-system git is used even without developer tools ---------------
lab=$(mklab)
printf '#!/bin/sh\nexit 2\n' > "$lab/bin/xcode-select"; chmod +x "$lab/bin/xcode-select"
out=$(PATH="$lab/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a NON-system git is used even without developer tools" "$out" "$lab/bin/git"

# --- 2. it never EXECUTED the candidate to decide ----------------------------
if [ -f "$lab/stub-ran" ]; then
  bad "resolver must not execute a candidate to probe it" "$(cat "$lab/stub-ran")"
else
  ok "resolver never executed the candidate"
fi

# --- 3. the real contract: system dir + no CLT -> EMPTY ---------------------
# Uses the genuine /usr/bin/git, with only xcode-select faked to fail — the
# exact scenario keweichen flagged: bare `git` on a toolchain-free host must
# not run at all, let alone twice per Stop.
lab3=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab3/xcode-select"; chmod +x "$lab3/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab3:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "macOS + system git + NO developer tools -> refuses (empty)" "$out" ""

# --- 4. ...and with the tools present it IS returned -------------------------
lab4=$(mktemp -d)
printf '#!/bin/sh\nexit 0\n' > "$lab4/xcode-select"; chmod +x "$lab4/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab4:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
if [ -n "$out" ]; then ok "macOS + system git + developer tools -> returned"
else bad "macOS + system git + developer tools -> returned" "got empty"; fi

# --- 5. NON-Darwin: the stub rule must not apply -----------------------------
lab5=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab5/xcode-select"; chmod +x "$lab5/xcode-select"
out=$(OSTYPE=linux-gnu PATH="$lab5:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
if [ -n "$out" ]; then ok "non-Darwin: system git is used (stub rule is macOS-only)"
else bad "non-Darwin: system git is used (stub rule is macOS-only)" "got empty"; fi

# --- 6. the REAL stub FIRST on PATH must not hide a real git further along --
# src/git_binary.py's select_git walks every PATH candidate rather than trusting
# the first match (@john-the-dev, #2469) — pin the shell twin the same way.
# /usr/bin genuinely comes first here; a resolver that only checked PATH's
# first hit would stop at the (undeveloper-tooled) stub and return empty.
lab6=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab6/xcode-select"; chmod +x "$lab6/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab6:/usr/bin:$lab/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a real stub earlier on PATH does not hide a real git further along" "$out" "$lab/bin/git"

# --- 7. A SYMLINK to the system stub must be refused, not accepted as "real" -
# keweichen, #4323 round 2: the old check compared only the candidate's
# DIRECTORY, so $HOME/bin/git -> /usr/bin/git looked like an ordinary PATH
# git and was returned even with no developer tools -- the hook then executed
# the actual stub through the symlink.
lab7=$(mktemp -d)
mkdir -p "$lab7/bin"
ln -s /usr/bin/git "$lab7/bin/git"
printf '#!/bin/sh\nexit 2\n' > "$lab7/xcode-select"; chmod +x "$lab7/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab7:$lab7/bin:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a symlink to the system stub is refused like the stub itself" "$out" ""

# --- 7b. same case, but with readlink UNAVAILABLE -- must still refuse, ------
# never fall through with an unresolved/corrupted path (the exact "dirname:
# command not found" shape python-binary.sh already hit and fixed).
out=$(OSTYPE=darwin25 PATH="$lab7:$lab7/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git" 2>/dev/null)
check "a symlink-to-stub is refused even when readlink itself is unavailable" "$out" ""

# --- 7c. a symlink chain LONGER than the internal bound must FAIL, never -----
# silently return an unresolved intermediate (keweichen, #4323 round 3: the
# old bound stopped at 20 hops without checking whether the target was still
# a symlink, so a chain longer than that -- macOS permits up to 32 -- fell
# through as "resolved" to a mid-chain link that trivially wasn't the stub).
# Tested directly against _sutando_git_realpath/_sutando_git_is_system_stub,
# not through resolve_git's outer PATH walk: that walk's own `[ -f ]` gate
# follows symlinks via the OS's native (and here, LOWER-than-32) ELOOP limit,
# so a chain built to exceed the code's 40-hop bound can get rejected by the
# OS before ever reaching the code under test -- a false pass for the wrong
# reason. Calling the internal functions directly removes that confound.
lab7c=$(mktemp -d)
_prev=/usr/bin/git
for _n in $(seq 1 45); do
  ln -s "$_prev" "$lab7c/l$_n"
  _prev="$lab7c/l$_n"
done
out=$(bash -c ". '$REPO/scripts/git-binary.sh'; _sutando_git_realpath '$lab7c/l45'" 2>/dev/null)
rc=$?
check "a 45-hop chain (over the internal bound) resolves to NOTHING, not an intermediate" "$out" ""
[ "$rc" -ne 0 ] && ok "...and reports failure (non-zero), not a false success" || bad "...and reports failure (non-zero), not a false success" "rc=$rc"

# --- 8. a DIRECTORY named "git" on PATH must never be returned as a binary ---
# `[ -x dir ]` is true for any traversable directory, which is not a git
# executable; `-f` must gate every candidate. OSTYPE is PINNED to darwin --
# without it (keweichen, #4323 round 3) this took the non-Darwin branch on
# CI, which is a bare `command -v git` with NO directory rejection at all,
# so it "passed" by finding a real /bin/git rather than by testing anything.
lab8=$(mktemp -d)
mkdir -p "$lab8/bin/git"
printf '#!/bin/sh\nexit 2\n' > "$lab8/xcode-select"; chmod +x "$lab8/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab8:$lab8/bin:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a directory literally named git is never returned" "$out" ""

# --- 8b. POSITIVE CONTROL: a symlink to a REAL non-system git IS accepted --
# keweichen, #4323 round 3: without this, "reject every symlink" would also
# pass case 7 above -- the fix must discriminate, not just refuse more.
lab8b=$(mklab)
mkdir -p "$lab8b/linkdir"
ln -s "$lab8b/bin/git" "$lab8b/linkdir/git"
printf '#!/bin/sh\nexit 2\n' > "$lab8b/xcode-select"; chmod +x "$lab8b/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab8b:$lab8b/linkdir:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a symlink to a real non-system git is accepted" "$out" "$lab8b/linkdir/git"

# --- 9. check-pending-tasks.sh sources the resolver, not a bare `git` --------
if grep -v '^\s*#' "$REPO/src/check-pending-tasks.sh" | grep -qE '(^|[^"$])\bgit -C'; then
  bad "check-pending-tasks.sh must not call a bare git" "found an unresolved git invocation"
else
  ok "check-pending-tasks.sh has no bare git invocation"
fi
grep -q 'scripts/git-binary.sh' "$REPO/src/check-pending-tasks.sh" && \
  ok "check-pending-tasks.sh sources git-binary.sh" || \
  bad "check-pending-tasks.sh sources git-binary.sh" "source line missing"

if [ "$fail" -eq 0 ]; then echo "PASS ($pass/$((pass+fail)))"; else echo "FAIL ($fail failed)"; fi
exit "$fail"

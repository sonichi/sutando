#!/usr/bin/env bash
# Contract test for scripts/git-binary.sh: never execute a candidate to decide
# whether it is usable; only `xcode-select -p` is a safe probe. Exit 0 = pass.
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

# --- 3. system dir + no CLT -> EMPTY -- genuine /usr/bin/git, xcode-select faked to fail.
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

# --- 6. a real stub FIRST on PATH must not hide a real git further along --
# /usr/bin comes first here; a first-hit-only resolver would stop there.
lab6=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab6/xcode-select"; chmod +x "$lab6/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab6:/usr/bin:$lab/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a real stub earlier on PATH does not hide a real git further along" "$out" "$lab/bin/git"

# --- 7. A SYMLINK to the system stub must be refused, not accepted as "real" -
# $HOME/bin/git -> /usr/bin/git must not look like an ordinary PATH git.
lab7=$(mktemp -d)
mkdir -p "$lab7/bin"
ln -s /usr/bin/git "$lab7/bin/git"
printf '#!/bin/sh\nexit 2\n' > "$lab7/xcode-select"; chmod +x "$lab7/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab7:$lab7/bin:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a symlink to the system stub is refused like the stub itself" "$out" ""

# --- 7b. same case, readlink UNAVAILABLE -- must still refuse, never fall
# through with an unresolved/corrupted path.
out=$(OSTYPE=darwin25 PATH="$lab7:$lab7/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git" 2>/dev/null)
check "a symlink-to-stub is refused even when readlink itself is unavailable" "$out" ""

# --- 7c. a chain LONGER than the internal bound must FAIL, never return an
# unresolved intermediate -- tested directly, past resolve_git's own ELOOP gate.
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

# --- 8. a DIRECTORY named "git" must never return as a binary -- OSTYPE pinned,
# since CI's non-Darwin branch has no directory rejection and would pass wrongly.
lab8=$(mktemp -d)
mkdir -p "$lab8/bin/git"
printf '#!/bin/sh\nexit 2\n' > "$lab8/xcode-select"; chmod +x "$lab8/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab8:$lab8/bin:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a directory literally named git is never returned" "$out" ""

# --- 8b. POSITIVE CONTROL: a symlink to a REAL non-system git IS accepted --
# without this, "reject every symlink" would also pass case 7 above.
lab8b=$(mklab)
mkdir -p "$lab8b/linkdir"
ln -s "$lab8b/bin/git" "$lab8b/linkdir/git"
printf '#!/bin/sh\nexit 2\n' > "$lab8b/xcode-select"; chmod +x "$lab8b/xcode-select"
out=$(OSTYPE=darwin25 PATH="$lab8b:$lab8b/linkdir:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
check "a symlink to a real non-system git is accepted" "$out" "$lab8b/linkdir/git"

# --- 9. check-pending-tasks.sh sources the resolver, not a bare `git` -- match
# any command-substitution invoking `git`, not just a -C'd probe.
if grep -v '^\s*#' "$REPO/src/check-pending-tasks.sh" | grep -qE '\$\(git[[:space:]]'; then
  bad "check-pending-tasks.sh must not call a bare git" "found an unresolved git invocation"
else
  ok "check-pending-tasks.sh has no bare git invocation"
fi
grep -q 'scripts/git-binary.sh' "$REPO/src/check-pending-tasks.sh" && \
  ok "check-pending-tasks.sh sources git-binary.sh" || \
  bad "check-pending-tasks.sh sources git-binary.sh" "source line missing"

# --- 10. the discovered stub candidate is NEVER executed to decide dev-tools -
# activated classifier/dev-tools witnesses: ran.log absence alone can't tell a refusal from a bypassed seam.
lab10=$(mktemp -d)
mkdir -p "$lab10/bin"
printf '#!/bin/sh\necho "RAN $*" >> %s/ran.log\nexit 1\n' "$lab10" > "$lab10/bin/git"
chmod +x "$lab10/bin/git"
printf '#!/bin/sh\necho "PROBED" >> %s/xcode-probed.log\nexit 2\n' "$lab10" > "$lab10/xcode-select"
chmod +x "$lab10/xcode-select"
# POSITIVE CONTROL on the recorder itself, before trusting any absence below.
"$lab10/bin/git" --version >/dev/null 2>&1
if [ -f "$lab10/ran.log" ]; then
  ok "the recording stub itself writes ran.log when actually executed"
else
  bad "the recording stub itself writes ran.log when actually executed" "no ran.log after a direct call"
fi
rm -f "$lab10/ran.log"
lab10_out="$lab10/resolve-stdout.txt"
OSTYPE=darwin25 PATH="$lab10/bin:$lab10:$PATH" /bin/bash -c "
  . '$REPO/scripts/git-binary.sh'
  touch '$lab10/classifier-invoked.log'
  _sutando_git_is_system_stub() { echo 1 >> '$lab10/classifier-invoked.log'; return 0; }
  resolve_git
" >"$lab10_out"
if [ -f "$lab10/ran.log" ]; then
  bad "the stub candidate is never executed to decide" "$(cat "$lab10/ran.log")"
elif [ ! -s "$lab10/classifier-invoked.log" ]; then
  bad "the stub candidate is never executed to decide" \
    "classifier override was never called -- ran.log's absence proves nothing (the seam was bypassed, not exercised)"
elif [ ! -f "$lab10/xcode-probed.log" ]; then
  bad "the stub candidate is never executed to decide" \
    "xcode-select was never probed -- the dev-tools branch this case targets was never reached"
elif [ -s "$lab10_out" ]; then
  bad "the stub candidate is never executed to decide" \
    "resolve_git returned non-empty ($(cat "$lab10_out")) -- a classified stub with no dev tools must refuse, not return a path"
else
  ok "the stub candidate is never executed to decide (classifier + dev-tools probe both confirmed invoked, resolver confirmed empty)"
fi

# --- 11. a CASE-VARIANT spelling of the real system stub must not bypass the
# guard -- realpath does not case-fold, only device+inode identity does.
if [ -f "/USR/BIN/git" ]; then
  lab11=$(mktemp -d)
  printf '#!/bin/sh\nexit 2\n' > "$lab11/xcode-select"; chmod +x "$lab11/xcode-select"
  out=$(OSTYPE=darwin25 PATH="$lab11:/USR/BIN:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
  check "a case-variant PATH spelling of the system stub still refuses without dev tools" "$out" ""

  printf '#!/bin/sh\nexit 0\n' > "$lab11/xcode-select"
  out=$(OSTYPE=darwin25 PATH="$lab11:/USR/BIN:/bin" /bin/bash -c ". '$REPO/scripts/git-binary.sh'; resolve_git")
  if [ -n "$out" ]; then ok "...and is usable once dev tools ARE present"
  else bad "...and is usable once dev tools ARE present" "got empty"; fi
else
  echo "  skip case-variant-alias test (host filesystem is case-sensitive)"
fi

if [ "$fail" -eq 0 ]; then echo "PASS ($pass/$((pass+fail)))"; else echo "FAIL ($fail failed)"; fi
exit "$fail"

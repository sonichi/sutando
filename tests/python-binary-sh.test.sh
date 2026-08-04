#!/usr/bin/env bash
# Contract test for scripts/python-binary.sh — the shell twin of
# src/python-binary.ts and src/git_binary.py.
#
# The rule being pinned: NEVER execute a candidate to decide whether it is
# usable. On a Mac without the Xcode Command Line Tools, /usr/bin/python3 is
# Apple's stub (one inode hardlinked across 78 names); executing it raises a
# modal install dialog BEFORE it can fail, so a probe like
#
#     "$candidate" -c "pass"
#
# is itself the bug. Only `xcode-select -p` is safe — /usr/bin/xcode-select is a
# real binary, so asking it never prompts.
#
# Run: bash tests/python-binary-sh.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()   { printf "  ok   %s\n" "$1"; pass=$((pass+1)); }
bad()  { printf "  FAIL %s — %s\n" "$1" "${2:-}"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3], got [$2]"; fi; }

# A sandbox whose PATH contains ONLY a fake system bin, so `command -v python3`
# finds our stand-in for the stub and nothing else.
mklab() {
  d=$(mktemp -d)
  mkdir -p "$d/bin"
  printf '#!/bin/sh\necho "STUB RAN" >> %s/stub-ran\nexit 1\n' "$d" > "$d/bin/python3"
  chmod +x "$d/bin/python3"
  printf '%s' "$d"
}

# --- 1. no developer tools -> refuses the system interpreter -----------------
lab=$(mklab)
printf '#!/bin/sh\nexit 2\n' > "$lab/bin/xcode-select"; chmod +x "$lab/bin/xcode-select"
# Make the fake python3 look like it lives in the system bin dir by pointing the
# resolver's directory comparison at our sandbox is not possible without editing
# it, so instead assert the REAL contract on the real system path below (test 4)
# and here assert the safe-probe behaviour with a non-system dir: a non-stub
# location must be accepted even with no toolchain.
out=$(PATH="$lab/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "a NON-system python is used even without developer tools" "$out" "$lab/bin/python3"

# --- 2. it never EXECUTED the candidate to decide ----------------------------
if [ -f "$lab/stub-ran" ]; then
  bad "resolver must not execute a candidate to probe it" "$(cat "$lab/stub-ran")"
else
  ok "resolver never executed the candidate (the #1789 probe shape would have)"
fi

# --- 3. $SUTANDO_PY wins, and only when executable ---------------------------
lab2=$(mklab)
printf '#!/bin/sh\nexit 0\n' > "$lab2/explicit"; chmod +x "$lab2/explicit"
out=$(SUTANDO_PY="$lab2/explicit" PATH="$lab2/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "\$SUTANDO_PY takes precedence" "$out" "$lab2/explicit"
out=$(SUTANDO_PY="$lab2/does-not-exist" PATH="$lab2/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "a non-executable \$SUTANDO_PY is ignored" "$out" "$lab2/bin/python3"

# --- 4. the real contract: system dir + no CLT -> EMPTY ----------------------
# Uses the genuine system path, with only xcode-select faked to fail. This is
# the case the whole change exists for.
lab3=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$lab3/xcode-select"; chmod +x "$lab3/xcode-select"
out=$(PATH="$lab3:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
check "system python + NO developer tools -> refuses (empty)" "$out" ""

# --- 5. ...and with the tools present it IS returned -------------------------
if xcode-select -p >/dev/null 2>&1; then
  out=$(PATH="/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
  if [ -n "$out" ]; then ok "system python + developer tools -> returned"
  else bad "system python + developer tools -> returned" "got empty"; fi
else
  printf "  skip developer tools absent on this host — case 5 not exercised\n"
fi

# --- 5b. NON-Darwin: the stub rule must not apply --------------------------
# The rule is a macOS artifact. On Linux /usr/bin/python3 is an ordinary
# interpreter and xcode-select does not exist, so applying it everywhere
# returned EMPTY and every caller broke with
#   sutando-config.sh: line 56: : command not found
# This suite only ever ran on macOS, so CI caught it and the tests did not.
lab5=$(mktemp -d)
printf '#!/bin/sh\necho Linux\n' > "$lab5/uname"; chmod +x "$lab5/uname"
printf '#!/bin/sh\nexit 2\n' > "$lab5/xcode-select"; chmod +x "$lab5/xcode-select"
out=$(PATH="$lab5:/usr/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$REPO'")
if [ -n "$out" ]; then ok "non-Darwin: system python is used (stub rule is macOS-only)"
else bad "non-Darwin: system python is used (stub rule is macOS-only)" "got empty — callers break"; fi

# --- 6. bundled runtime beats PATH ------------------------------------------
lab4=$(mklab)
mkdir -p "$lab4/engine/../runtime/python/bin"
printf '#!/bin/sh\nexit 0\n' > "$lab4/runtime/python/bin/python3"
chmod +x "$lab4/runtime/python/bin/python3"
mkdir -p "$lab4/engine"
out=$(PATH="$lab4/bin:/bin" /bin/bash -c ". '$REPO/scripts/python-binary.sh'; resolve_python '$lab4/engine'")
check "bundled <engine>/../runtime/python wins over PATH" "$out" "$lab4/engine/../runtime/python/bin/python3"

# --- 7. every caller that used to fall through to the bare name is routed ----
for f in src/startup.sh scripts/sutando-config.sh src/agent/claude/cli/start-cli.sh; do
  if grep -qE '^\s*PY="python3"\s*$' "$REPO/$f"; then
    bad "$f has no bare-name fallthrough" 'PY="python3" still present'
  else
    ok "$f has no bare-name fallthrough"
  fi
done


# --- 8. CALLER-LEVEL: no caller may degrade into `"" -c ...` ----------------
# Resolver unit coverage is not enough (CR #2599, @john-the-dev): the advertised
# "returns empty, callers skip" contract has to hold at the ACTIVATED entry
# points. Before this, sutando-config.sh exited 127 with the shell's opaque
#   scripts/sutando-config.sh: line 56: : command not found
# once per call site, and startup.sh promised services "will be skipped" while
# actually aborting.
noclt=$(mktemp -d)
printf '#!/bin/sh\nexit 2\n' > "$noclt/xcode-select"; chmod +x "$noclt/xcode-select"

out=$(env -u SUTANDO_PY PATH="$noclt:/usr/bin:/bin" /bin/bash "$REPO/scripts/sutando-config.sh" workspace 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
  ok "sutando-config.sh: succeeds when a python IS resolvable"
else
  case "$out" in
    *"command not found"*) bad "sutando-config.sh fails ACTIONABLY, not with ': command not found'" "$out" ;;
    *"no runnable python3"*) ok "sutando-config.sh fails once, actionably (exit $rc)" ;;
    *) bad "sutando-config.sh fails actionably" "unexpected: $out" ;;
  esac
fi

# The startup message must not promise a skip the script does not perform.
if grep -q 'will be skipped' "$REPO/src/startup.sh"; then
  for svc in dashboard agent-api; do
    if grep -qE "skipped \(no runnable python3\)" "$REPO/src/startup.sh"; then
      ok "startup.sh actually skips $svc rather than only claiming to"
      break
    else
      bad "startup.sh actually skips $svc" "promise without a skip branch"
      break
    fi
  done
fi

printf "\npassed=%d failed=%d\n" "$pass" "$fail"
[ "$fail" -eq 0 ]

#!/usr/bin/env bash
# Contract test for scripts/core-status.sh's INTERPRETER RESOLUTION.
#
# The wrapper runs on every status transition (AGENTS.md, proactive-loop
# SKILL.md). It used to `exec python3`, which ignores $SUTANDO_PY and the
# bundled runtime. On a Mac without Command Line Tools the PATH python3 is
# Apple's stub: executing it raises the install modal, so the failure is a
# repeated dialog AND a core-status.json that stops advancing — removing the
# fresh-busy evidence graceful-restart reads before authorising a kill.
#
# Run: bash tests/core-status-sh-python-resolution.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok()  { printf "  ok   %s\n" "$1"; pass=$((pass+1)); }
bad() { printf "  FAIL %s — %s\n" "$1" "${2:-}"; fail=$((fail+1)); }

# A poisoned `python3` FIRST on PATH — what `command -v python3` finds on a Mac
# without Command Line Tools. It records having been run, then fails. Touching
# it at all is the defect; system dirs stay reachable so bash/dirname resolve.
lab=$(mktemp -d)
mkdir -p "$lab/bin" "$lab/ws/state"
printf '#!/bin/sh\necho ran >> "%s/stub-ran"\nexit 1\n' "$lab" > "$lab/bin/python3"
chmod +x "$lab/bin/python3"

# A REAL interpreter offered via SUTANDO_PY, the way the desktop launcher does.
REAL_PY="$(command -v python3 || true)"
if [ -z "$REAL_PY" ]; then
	echo "  skip — no real python3 on this host to offer as SUTANDO_PY"
	exit 0
fi

out=$(cd "$REPO" && env PATH="$lab/bin:/usr/bin:/bin" \
	SUTANDO_PY="$REAL_PY" \
	SUTANDO_WORKSPACE_OVERRIDE="$lab/ws" \
	bash scripts/core-status.sh running "resolution probe" 2>&1)
rc=$?

if [ -f "$lab/stub-ran" ]; then
	bad "the poisoned PATH python3 is never executed" "stub ran $(wc -l < "$lab/stub-ran") time(s)"
else
	ok "the poisoned PATH python3 is never executed"
fi

if [ "$rc" -eq 0 ]; then
	ok "wrapper succeeds when SUTANDO_PY names a runnable interpreter"
else
	bad "wrapper succeeds when SUTANDO_PY names a runnable interpreter" "rc=$rc out=$out"
fi

# A repo path containing a double quote would break an unquoted heredoc that
# splices REPO_ROOT into Python source. Passing it through argv cannot.
if grep -q "<<'PY'" "$REPO/scripts/core-status.sh"; then
	ok "heredoc is quoted, so REPO_ROOT is data rather than spliced source"
else
	bad "heredoc is quoted" "found an interpolating heredoc"
fi

if grep -qE '^exec[[:space:]]+python3' "$REPO/scripts/core-status.sh"; then
	bad "no bare 'exec python3' remains" "the wrapper still bypasses the resolver"
else
	ok "no bare 'exec python3' remains"
fi

rm -rf "$lab"
printf "\n"
if [ "$fail" -gt 0 ]; then
	printf "FAIL — %d of %d\n" "$fail" "$((pass + fail))"
	exit 1
fi
printf "PASS — %d/%d core-status.sh resolution checks\n" "$pass" "$pass"

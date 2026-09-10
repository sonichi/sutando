#!/usr/bin/env bash
# PATTERN_ENV must catch every `$SUTANDO_WORKSPACE` read form its own header claims.
#
# The header comment lists `os.environ["SUTANDO_WORKSPACE"]` among the forms this lint
# detects, but the regex required a PAREN after `os.environ` — so the subscript form,
# the most idiomatic of the three in Python, passed the `--diff` gate silently. Doc and
# regex disagreed, and the gate is what CI runs.
#
# Controls, in --diff mode because that is the branch that exits 1:
#
#   1. subscript form            FAILS   (the defect this pins)
#   2. .get() form               FAILS   (the arm that already worked — proves the
#                                         harness can produce a hit, so control 1's
#                                         failure is about the regex, not the fixture)
#   3. os.getenv() form          FAILS   (third documented form)
#   4. a clean file              PASSES  (the lint is not simply failing everything —
#                                         without this, a regex of `.` would score 3/3)
#   5. a test under tests/<sub>/ PASSES  (widening the pattern must not start gating
#                                         test files the ALLOWED policy already exempts)
#
# Control 4 is the one that matters for reading the set at a glance: three FAILs prove
# nothing on their own, because a pattern that matches everything produces them too.
#
# Run: bash tests/lint-workspace-resolution-env-forms.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINT="$REPO/scripts/lint-workspace-resolution.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

scaffold() {
  rm -rf "$TMP/r"; mkdir -p "$TMP/r/scripts" "$TMP/r/src" "$TMP/r/tests/observability"
  cp "$LINT" "$TMP/r/scripts/lint-workspace-resolution.sh"
  cd "$TMP/r" || exit 9
  git init -q . && git config user.email t@t && git config user.name t
  printf 'placeholder\n' > README.md
  git add -A && git commit -qm base
  git branch -qM main
  # Probe commits must sit off the base ref, or `git diff main...HEAD` is empty and
  # every control passes vacuously.
  git checkout -q -b probe
}

verdict() {  # $1 = repo-relative path, $2 = body -> exit code
  printf '%s' "$2" > "$TMP/r/$1"
  ( cd "$TMP/r" && git add -A && git commit -qm probe >/dev/null 2>&1 )
  ( cd "$TMP/r" && BASE_REF=main bash scripts/lint-workspace-resolution.sh --diff >/dev/null 2>&1; echo $? )
}

echo "lint-workspace-resolution: every documented env-read form"

PROBE="src/probe_reader.py"

scaffold
rc="$(verdict "$PROBE" 'import os
ws = os.environ["SUTANDO_WORKSPACE"]
')"
[ "$rc" = 1 ] && ok 'subscript os.environ["SUTANDO_WORKSPACE"] is refused' \
              || bad 'subscript form' "rc=$rc (expected 1)"

scaffold
rc="$(verdict "$PROBE" 'import os
ws = os.environ.get("SUTANDO_WORKSPACE")
')"
[ "$rc" = 1 ] && ok 'os.environ.get("SUTANDO_WORKSPACE") is refused' \
              || bad '.get form' "rc=$rc (expected 1)"

scaffold
rc="$(verdict "$PROBE" 'import os
ws = os.getenv("SUTANDO_WORKSPACE")
')"
[ "$rc" = 1 ] && ok 'os.getenv("SUTANDO_WORKSPACE") is refused' \
              || bad 'getenv form' "rc=$rc (expected 1)"

scaffold
rc="$(verdict "$PROBE" 'import os
ws = os.environ["SOME_OTHER_VAR"]
')"
[ "$rc" = 0 ] && ok 'an unrelated env read still passes (pattern is not matching everything)' \
              || bad 'clean file' "rc=$rc (expected 0)"

scaffold
rc="$(verdict "tests/observability/probe.test.py" 'import os
ws = os.environ["SUTANDO_WORKSPACE"]
')"
[ "$rc" = 0 ] && ok 'a test under tests/<subdir>/ stays exempt' \
              || bad 'tests subdir exemption' "rc=$rc (expected 0)"

cd "$REPO" || exit 9
if [ "$fails" -eq 0 ]; then echo "lint env-forms: all checks pass"; exit 0; fi
echo "lint env-forms: $fails check(s) failed"; exit 1

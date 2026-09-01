#!/usr/bin/env bash
# The line-scoped `allow-repo-root` pragma must EXEMPT one line and BLIND nothing else.
#
# @john-the-dev on #2639 showed why a file-level ALLOWED entry is not an exemption but a
# blind spot: they added a real `Path(__file__).resolve().parent.parent / "workspace"`
# into an allowlisted file and the lint still exited 0. The positive control shipped with
# that commit only proved a DIFFERENT, non-allowlisted file was still visible — it never
# exercised the hole introduced for the allowlisted one.
#
# So the controls here are deliberately about the SAME file the pragma appears in:
#
#   1. the pragma'd repo-root line passes                        (the exemption works)
#   2. a second prohibited expression in that same file FAILS    (john's exact experiment)
#   3. a SPOOFED pragma on a workspace derivation FAILS          (the pragma cannot hide
#                                                                 what the lint exists for)
#   4. an unrelated non-allowlisted file still FAILS             (nothing globally broken)
#
# Control 3 is the one that caught a real defect in the first cut of this fix: the pragma
# excused any line carrying it, so it could have hidden a genuine workspace resolution —
# the file-level blind spot rebuilt at line granularity.
#
# Run: bash tests/lint-workspace-resolution-pragma.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LINT="$REPO/scripts/lint-workspace-resolution.sh"
PRAGMA='lint-workspace-resolution: allow-repo-root'
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# A throwaway git repo so `--diff` has a base to compare against.
scaffold() {
  rm -rf "$TMP/r"; mkdir -p "$TMP/r/scripts" "$TMP/r/src"
  cp "$LINT" "$TMP/r/scripts/lint-workspace-resolution.sh"
  cd "$TMP/r" || exit 9
  git init -q . && git config user.email t@t && git config user.name t
  printf 'placeholder\n' > README.md
  git add -A && git commit -qm base
  git branch -qM main
  # The probe commit MUST live on a branch other than the base ref, or
  # `git diff main...HEAD` is empty and every control passes vacuously — which is
  # exactly what control 4 caught in the first draft of this harness.
  git checkout -q -b probe
}

# Run the lint over a candidate file added on top of `main`; echo its exit code.
verdict() {  # $1 = repo-relative path, $2 = file body
  printf '%s' "$2" > "$TMP/r/$1"
  ( cd "$TMP/r" && git add -A && git commit -qm probe >/dev/null 2>&1 )
  ( cd "$TMP/r" && BASE_REF=main bash scripts/lint-workspace-resolution.sh --diff >/dev/null 2>&1; echo $? )
}

echo "lint-workspace-resolution: line-scoped pragma"

# The pragma is only honoured for files the ALLOWED regex does not already cover, so use
# the real guard path — that is the file the exemption was created for.
GUARD="scripts/hermetic-workspace-guard.py"

scaffold
rc="$(verdict "$GUARD" "from pathlib import Path
REPO = Path(__file__).resolve().parent.parent  # $PRAGMA
")"
[ "$rc" = 0 ] && ok "1. a pragma'd repo-root anchor is exempt" \
              || bad "1. a pragma'd repo-root anchor is exempt" "exit=$rc"

scaffold
rc="$(verdict "$GUARD" "from pathlib import Path
REPO = Path(__file__).resolve().parent.parent  # $PRAGMA
_TMP = Path(__file__).resolve().parent.parent / \"workspace\"
")"
[ "$rc" != 0 ] && ok "2. a SECOND prohibited expression in the same file still fails" \
               || bad "2. a SECOND prohibited expression in the same file still fails" \
                      "lint exited 0 — the file is blind, which is the #2639 defect"

scaffold
rc="$(verdict "$GUARD" "from pathlib import Path
_WS = Path(__file__).resolve().parent.parent / \"workspace\"  # $PRAGMA
")"
[ "$rc" != 0 ] && ok "3. a SPOOFED pragma cannot excuse a workspace derivation" \
               || bad "3. a SPOOFED pragma cannot excuse a workspace derivation" \
                      "the pragma became a way to hide the thing being linted"

scaffold
rc="$(verdict "src/zz_unrelated.py" "from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
")"
[ "$rc" != 0 ] && ok "4. POSITIVE CONTROL — an unrelated file is still caught" \
               || bad "4. POSITIVE CONTROL — an unrelated file is still caught" \
                      "the lint can no longer produce a positive at all"

echo
if [ "$fails" -ne 0 ]; then echo "FAILED ($fails)"; exit 1; fi
echo "line-scoped pragma: all checks passed"

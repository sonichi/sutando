#!/usr/bin/env bash
# A PR-draft artifact committed to the repo root must fail the review gate.
# Nothing gated this: it is a diff HEADER, so the hardcoded-paths content
# scanner cannot see it however its patterns are written.
#
# Run: bash tests/review-checks-root-artifacts.test.sh
# Exit: 0 = all pass, 1 = failure
set -uo pipefail

REPO="${REPO_UNDER_TEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RC="$REPO/scripts/review-checks.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails + 1)); }

echo "review-checks root-artifacts:"

added_file_diff() {  # $1 = path
    printf 'diff --git a/%s b/%s\nnew file mode 100644\nindex 0000000..1111111\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1 @@\n+placeholder body\n' "$1" "$1" "$1"
}

run_rc() { printf '%s' "$1" | bash "$RC" 2>&1; }

# --- the reported failure: prbody.md / reply1.md at the root -----------------
for f in prbody.md reply1.md; do
    out="$(run_rc "$(added_file_diff "$f")")"; rc=$?
    if [[ $rc -eq 1 && "$out" == *"$f"* && "$out" == *"root-artifacts"* ]]; then
        ok "$f at the root fails the gate"
    else
        bad "$f at the root fails the gate" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
    fi
done

# --- root-ONLY scoping: the same name under tests/ must pass -----------------
# Without this the rule reaches legitimate fixtures and gets disabled.
out="$(run_rc "$(added_file_diff tests/fixtures/reply1.md)")"; rc=$?
if [[ $rc -eq 0 ]]; then
    ok "the same name under tests/ is NOT flagged"
else
    bad "the same name under tests/ is NOT flagged" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# --- a legitimate new root file must still pass -----------------------------
out="$(run_rc "$(added_file_diff CHANGELOG.md)")"; rc=$?
if [[ $rc -eq 0 ]]; then
    ok "an unmatched new root file passes"
else
    bad "an unmatched new root file passes" "rc=$rc"
fi

# --- deleting an artifact must pass: only additions strand one --------------
del="$(printf 'diff --git a/prbody.md b/prbody.md\ndeleted file mode 100644\nindex 1111111..0000000\n--- a/prbody.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-placeholder body\n')"
out="$(run_rc "$del")"; rc=$?
if [[ $rc -eq 0 ]]; then
    ok "deleting the artifact passes"
else
    bad "deleting the artifact passes" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# --- MODIFYING an existing root file is not an addition ---------------------
mod="$(printf 'diff --git a/README.md b/README.md\nindex 1111111..2222222 100644\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n line\n+added line\n')"
out="$(run_rc "$mod")"; rc=$?
if [[ $rc -eq 0 ]]; then
    ok "modifying an existing root file passes"
else
    bad "modifying an existing root file passes" "rc=$rc"
fi

# --- the fence: hardcoded-paths must still fire -----------------------------
# Without this, "flag nothing" would satisfy every case above.
hp="$(printf 'diff --git a/src/x.py b/src/x.py\nindex 1111111..2222222 100644\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,2 @@\n line\n+P = "/Users/someone/thing"\n')"
out="$(run_rc "$hp")"; rc=$?
if [[ $rc -eq 1 && "$out" == *"hardcoded-paths"* ]]; then
    ok "hardcoded-paths still fires (this change did not disable it)"
else
    bad "hardcoded-paths still fires" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# --- a clean diff still passes ----------------------------------------------
clean="$(printf 'diff --git a/src/x.py b/src/x.py\nindex 1111111..2222222 100644\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,2 @@\n line\n+harmless = 1\n')"
out="$(run_rc "$clean")"; rc=$?
if [[ $rc -eq 0 && "$out" == *"PASS"* ]]; then
    ok "a clean diff still passes"
else
    bad "a clean diff still passes" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

if [[ $fails -eq 0 ]]; then echo "PASS"; exit 0; fi
echo "FAILED ($fails)"; exit 1

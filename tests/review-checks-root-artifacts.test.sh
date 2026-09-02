#!/usr/bin/env bash
# A PR-draft artifact at the repo root must fail the gate. Run:
# bash tests/review-checks-root-artifacts.test.sh   (0 = pass, 1 = failure)
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
# This fixture's index sha is synthetic, so prose-cap always skips and the
# verdict token is PARTIAL. What this suite asserts is only that nothing fired.
if [[ $rc -eq 0 && "$out" != *"FAIL"* ]]; then
    ok "a clean diff still passes"
else
    bad "a clean diff still passes" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# --- the gate must not switch itself off when the guide is thin ---------------
# Both printed "PASS ... clean" at rc=0 while scanning nothing.
run_guide() { printf '%s' "$2" | bash "$RC" --guide "$1" 2>&1; }
ART="$(added_file_diff prbody.md)"
HARDCODED="$(printf 'diff --git a/src/x.py b/src/x.py\nindex 1..2 100644\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,2 @@\n a\n+P = "/Users/someone/x"\n')"

out="$(run_guide /dev/null "$ART")"; rc=$?
if [[ $rc -eq 1 && "$out" == *"prbody.md"* ]]; then
    ok "a MISSING guide still gates root artifacts (defaults installed)"
else
    bad "a MISSING guide still gates root artifacts" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# A real guide that parses for hardcoded-paths but carries no root_artifact_glob.
NOKEY="$(mktemp)"; trap 'rm -f "$NOKEY"' EXIT
python3 - "$REPO/REVIEW.md" "$NOKEY" <<'PYEOF'
import re, sys, pathlib
t = pathlib.Path(sys.argv[1]).read_text()
out = re.sub(r"\n  root-artifacts:.*?(?=\n  [a-z-]+:\n|\Z)", "\n", t, flags=re.S)
assert "root_artifact_glob" not in out, "fixture still carries the key"
assert "hardcoded-paths:" in out, "fixture lost the control section"
pathlib.Path(sys.argv[2]).write_text(out)
PYEOF

out="$(run_guide "$NOKEY" "$ART")"; rc=$?
if [[ $rc -eq 1 && "$out" == *"prbody.md"* ]]; then
    ok "a guide MISSING the key still gates root artifacts"
else
    bad "a guide MISSING the key still gates root artifacts" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# The note is what tells an operator the defaults are in play rather than their
# own list; without it the substitution is silent.
out="$(run_guide "$NOKEY" "$ART")"
if [[ "$out" == *"root_artifact_glob"* && "$out" == *"default"* ]]; then
    ok "...and says so, so the substitution is not silent"
else
    bad "...and says so" "no default note in: $(printf '%s' "$out" | tr '\n' ' ')"
fi

# The fixture must still exercise the OTHER check, or these three prove nothing
# about a guide that parses — they would just be re-testing the missing-guide path.
out="$(run_guide "$NOKEY" "$HARDCODED")"; rc=$?
if [[ $rc -eq 1 && "$out" == *"hardcoded-paths"* && "$out" != *"used generic defaults"* ]]; then
    ok "control: the no-key guide DOES parse for hardcoded-paths"
else
    bad "control: the no-key guide DOES parse for hardcoded-paths" "rc=$rc out=$(printf '%s' "$out" | tr '\n' ' ')"
fi

# Defense in depth: the scanner itself must refuse an unconfigured run, so a
# future caller that forgets the env cannot resurrect the silent pass.
out="$(printf '%s' "$ART" | RC_ROOT_ARTIFACT_GLOBS="" python3 "$REPO/scripts/review-checks-root-artifacts.py" 2>&1)"; rc=$?
if [[ $rc -ne 0 ]]; then
    ok "the scanner refuses an unconfigured run rather than passing"
else
    bad "the scanner refuses an unconfigured run" "rc=$rc"
fi

if [[ $fails -eq 0 ]]; then echo "PASS"; exit 0; fi
echo "FAILED ($fails)"; exit 1

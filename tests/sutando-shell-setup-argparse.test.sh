#!/usr/bin/env bash
# Inline tests for sutando-shell-setup.sh argparse semantics + the new
# --from validation + user-function detection (Mini PR #1424 review #1+2+3
# follow-ups). Per `feedback_e2e_tests_for_contributions`: when a parser
# is touched, ship a regression to prevent silent breakage on the next
# argparse change.
#
# What this exercises:
#   1. Two-pass argparse — modifier-after-mode flag ordering works.
#   2. Two-pass argparse — modifier-before-mode flag ordering works.
#   3. No-modifier case still parses cleanly.
#   4. --from=/ rejected with explicit error.
#   5. --from=/etc rejected with shape-mismatch error.
#   6. --from=/nonexistent/.claude-sutando rejected with does-not-exist error.
#   7. user_defined_function_present catches all 7 fixture shapes.
#   8. user_defined_function_present does NOT catch 3 negative fixtures.
#
# Runs offline, uses /tmp, leaves no trace.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SETUP="$REPO/scripts/sutando-shell-setup.sh"

pass=0
fail=0

assert_pass() {
  local label="$1"
  pass=$((pass+1))
  echo "  ✓ $label"
}

assert_fail() {
  local label="$1"
  local detail="$2"
  fail=$((fail+1))
  echo "  ✗ $label" >&2
  echo "      $detail" >&2
}

assert_contains() {
  local label="$1"
  local haystack="$2"
  local needle="$3"
  # `--` terminates flag parsing so needles starting with `--` (like
  # `--from=...`) aren't mistaken for grep options. macOS BSD grep needs it.
  if printf '%s' "$haystack" | grep -qF -- "$needle"; then
    assert_pass "$label"
  else
    assert_fail "$label" "expected to contain: $needle"
  fi
}

assert_rc() {
  local label="$1"
  local expected="$2"
  local actual="$3"
  if [ "$expected" = "$actual" ]; then
    assert_pass "$label"
  else
    assert_fail "$label" "expected rc=$expected got rc=$actual"
  fi
}

# ----------------------------------------------------------------------
# Argparse tests (--from + --repair-paths in various orderings)
# ----------------------------------------------------------------------

echo "Argparse two-pass tests:"

# (1) modifier-after-mode: --repair-paths --from=PATH
out=$(bash "$SETUP" --repair-paths --from=/Users 2>&1) || true
# /Users alone is rejected (not .claude-shape) — proves --from was parsed
assert_contains "modifier-after-mode parses --from" "$out" "--from=/Users rejected — must end in"

# (2) modifier-before-mode: --from=PATH --repair-paths
out=$(bash "$SETUP" --from=/Users --repair-paths 2>&1) || true
assert_contains "modifier-before-mode parses --from" "$out" "--from=/Users rejected — must end in"

# (3) no modifier — regression check
out=$(bash "$SETUP" --repair-paths 2>&1) || true
assert_contains "no-modifier basic --repair-paths runs" "$out" "Re-pinning hardcoded paths"

# ----------------------------------------------------------------------
# --from= validation tests
# ----------------------------------------------------------------------

echo ""
echo "--from= validation tests:"

# (4) --from=/  → reject empty/root
out=$(bash "$SETUP" --repair-paths --from=/ 2>&1) || true
assert_contains "--from=/ rejected" "$out" "cannot be empty or root"

# (5) --from=/etc  → reject wrong shape
out=$(bash "$SETUP" --repair-paths --from=/etc 2>&1) || true
assert_contains "--from=/etc rejected (wrong shape)" "$out" "must end in /.claude or /.claude-sutando"

# (6) --from=/nonexistent/.claude-sutando  → reject nonexistent
out=$(bash "$SETUP" --repair-paths --from=/nonexistent/.claude-sutando 2>&1) || true
assert_contains "--from=/nonexistent/... rejected (no dir)" "$out" "directory does not exist"

# (7) --from=/nonexistent/.claude  → reject nonexistent (different shape, same outcome)
out=$(bash "$SETUP" --repair-paths --from=/nonexistent/.claude 2>&1) || true
assert_contains "--from=/nonexistent/.claude rejected (no dir)" "$out" "directory does not exist"

# ----------------------------------------------------------------------
# user_defined_function_present regex coverage (sourced helper)
# ----------------------------------------------------------------------

echo ""
echo "user_defined_function_present regex tests:"

# Reusable test harness: write fixture to temp rc, source helper logic,
# assert detection outcome.
test_detect() {
  local label="$1"
  local expected="$2"  # "DETECTED" or "miss"
  local content="$3"
  local TMP="$(mktemp -t shell-setup-detect.XXXXXX)"
  printf '%s\n' "$content" > "$TMP"
  result=$(RC_FILE="$TMP" bash -c '
MARKER_BEGIN="# >>> sutando-shell-setup managed block — do not edit between markers"
MARKER_END="# <<< sutando-shell-setup managed block"
user_defined_function_present() {
  [ -f "$RC_FILE" ] || return 1
  awk -v b="$MARKER_BEGIN" -v e="$MARKER_END" "
    \$0 == b { inblk=1; next }
    \$0 == e { inblk=0; next }
    !inblk { print }
  " "$RC_FILE" | grep -qE "^[[:space:]]*(claude-sutando[[:space:]]*\(\)|function[[:space:]]+claude-sutando([[:space:]]|\(|\{|$))"
}
user_defined_function_present && echo "DETECTED" || echo "miss"
')
  rm -f "$TMP"
  if [ "$result" = "$expected" ]; then
    assert_pass "$label"
  else
    assert_fail "$label" "expected=$expected got=$result"
  fi
}

# Positive fixtures (all should detect)
test_detect "[POS] column-0 POSIX"             "DETECTED" "claude-sutando() { echo hi; }"
test_detect "[POS] leading whitespace"         "DETECTED" "  claude-sutando() { echo hi; }"
test_detect "[POS] space before parens"        "DETECTED" "claude-sutando () { echo hi; }"
test_detect "[POS] function keyword + brace"   "DETECTED" "function claude-sutando { echo hi; }"
test_detect "[POS] function keyword + parens"  "DETECTED" "function claude-sutando () { echo hi; }"
test_detect "[POS] leading ws + function kw"   "DETECTED" "  function claude-sutando { echo hi; }"
test_detect "[POS] function kw bare (no brace)" "DETECTED" "$(printf 'function claude-sutando\n{\n  echo hi\n}')"

# Negative fixtures (all should miss)
test_detect "[NEG] alias only"                 "miss"     "alias claude-sutando='echo hi'"
test_detect "[NEG] no claude-sutando at all"   "miss"     "alias foo=bar"
test_detect "[NEG] similar but diff name"      "miss"     "claude-other() { echo hi; }"
test_detect "[NEG] function kw, diff name"     "miss"     "function claude-other { echo hi; }"

# ----------------------------------------------------------------------
# Brace-internal RC capture pattern test (Mini PR #1424 review #1)
# ----------------------------------------------------------------------
#
# This validates the FIX PATTERN used in scripts/sutando-migrate.sh for
# --merge-append + the two append blocks, not those functions directly
# (they're deeply nested in move_file_local() and hard to call in
# isolation). Proves the bitmask `||`-per-command pattern catches
# inner-step failures that brace-overall `||` would silently swallow.

echo ""
echo "Brace-internal RC capture (Mini #1) tests:"

# (a) All-pass case: every command in the brace succeeds → mask stays 0.
TMP="$(mktemp -d -t bracerc-test.XXXXXX)"
echo "src" > "$TMP/src"
echo "dst" > "$TMP/dst"
_err=0
{
    cat "$TMP/dst"      || _err=$((_err|1))
    echo ""             || _err=$((_err|2))
    echo "==="          || _err=$((_err|4))
    echo ""             || _err=$((_err|8))
    cat "$TMP/src"      || _err=$((_err|16))
} > "$TMP/out" 2>/dev/null
assert_rc "[brace] happy path → mask=0" "0" "$_err"
rm -rf "$TMP"

# (b) Early-cat fail: cat-dst missing, later cat-src OK. Brace-overall
#     exit would be 0 (last command). Bitmask should be 1.
TMP="$(mktemp -d -t bracerc-test.XXXXXX)"
echo "src" > "$TMP/src"
# Intentionally do NOT create $TMP/dst
_err=0
{
    cat "$TMP/dst"      || _err=$((_err|1))
    echo ""             || _err=$((_err|2))
    echo "==="          || _err=$((_err|4))
    echo ""             || _err=$((_err|8))
    cat "$TMP/src"      || _err=$((_err|16))
} > "$TMP/out" 2>/dev/null
assert_rc "[brace] cat-dst fail, cat-src OK → mask=1 (the gap Mini caught)" "1" "$_err"
rm -rf "$TMP"

# (c) Both cats fail: mask = 1 | 16 = 17.
TMP="$(mktemp -d -t bracerc-test.XXXXXX)"
# Intentionally do NOT create either dst or src
_err=0
{
    cat "$TMP/dst"      || _err=$((_err|1))
    echo ""             || _err=$((_err|2))
    echo "==="          || _err=$((_err|4))
    echo ""             || _err=$((_err|8))
    cat "$TMP/src"      || _err=$((_err|16))
} > "$TMP/out" 2>/dev/null
assert_rc "[brace] both cats fail → mask=17 (1|16)" "17" "$_err"
rm -rf "$TMP"

# (d) Brace-overall comparison: prove the OLD pattern (without per-command
#     capture) would have returned 0 for case (b). This is the regression
#     the per-command capture pattern prevents.
#
# Wrap in `set +e` / `set -e` because the brace fails (first cat misses)
# and the script's top-level `set -e` would otherwise propagate the exit
# up. We WANT to capture the brace's rc into a variable — that requires
# tolerating a non-zero rc temporarily.
TMP="$(mktemp -d -t bracerc-test.XXXXXX)"
echo "src" > "$TMP/src"
# Intentionally do NOT create $TMP/dst
set +e
{
    cat "$TMP/dst"     # no || capture — relies on brace-overall exit
    echo ""
    echo "==="
    echo ""
    cat "$TMP/src"
} > "$TMP/out" 2>/dev/null
brace_overall_rc=$?
set -e
assert_rc "[brace] old-pattern brace-overall rc=0 even with cat-dst fail (proves the gap)" "0" "$brace_overall_rc"
rm -rf "$TMP"

# ----------------------------------------------------------------------
# Import-UX tests: --import/--migrate alias, deprecation warning, exclude
# policy (post-this-PR work).
# ----------------------------------------------------------------------

echo ""
echo "Import-UX (--import / --migrate alias / weight-reduction excludes):"

# (a) --import is the canonical flag — invocation should NOT print
#     deprecation warning.
out=$(bash "$SETUP" --import 2>&1 </dev/null) || true
if printf '%s' "$out" | grep -qF -- "--migrate is deprecated"; then
  assert_fail "--import does NOT print deprecation warning" "found warning in output"
else
  assert_pass "--import does NOT print deprecation warning"
fi

# (b) --migrate works (still routes through) AND prints the deprecation warning
out=$(bash "$SETUP" --migrate 2>&1 </dev/null) || true
assert_contains "--migrate prints deprecation warning" "$out" "--migrate is deprecated"
assert_contains "--migrate routes to --import flow (header line)" "$out" "sutando-shell-setup --migrate"

# (c) INVOKED_AS echoes back what the user typed — --migrate user sees
#     '--migrate' in status, --import user sees '--import'.
out=$(bash "$SETUP" --import 2>&1 </dev/null) || true
assert_contains "--import echoes --import in status line" "$out" "sutando-shell-setup --import"

# (d) Weight-reduction excludes are present in the rsync-filter
#     declaration block (grep the source, not the runtime, since the dry-run
#     preview is truncated to 50 lines and may not show every filter).
filters_block=$(sed -n "/RSYNC_FILTERS+=(/,/^    )/p" "$SETUP")
for excl in "shell-snapshots/" "history.jsonl" "file-history/"; do
  if printf '%s' "$filters_block" | grep -qF -- "--exclude='$excl'"; then
    assert_pass "exclude '$excl' present in RSYNC_FILTERS"
  else
    assert_fail "exclude '$excl' missing from RSYNC_FILTERS" "see scripts/sutando-shell-setup.sh"
  fi
done

# (e) channels/*/*.env + channels/*/access.json.bak* are NOT in the
#     exclude list (the (B) copy + warn shift). The pre-fix versions had
#     these as excludes; reverting them is the core of this PR.
for not_excl in "channels/*/*.env" "channels/*/access.json.bak"; do
  if printf '%s' "$filters_block" | grep -qF -- "--exclude='$not_excl"; then
    assert_fail "exclude '$not_excl' should be ABSENT (copy + warn policy)" "see filter block"
  else
    assert_pass "exclude '$not_excl' absent (copy + warn policy)"
  fi
done

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------

echo ""
echo "------------------------------------------------------"
echo "  $pass passed, $fail failed"
echo "------------------------------------------------------"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0

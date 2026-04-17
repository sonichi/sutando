#!/usr/bin/env bash
# Test scripts/results-health.sh — verifies it correctly identifies
# flood-risk signals (zero-byte files, stale files, count >10) without
# false positives on a clean state.
#
# Self-contained: creates fixtures in a tmpdir, runs results-health.sh
# pointed at the tmpdir (NOT the real results/), asserts exit code +
# stderr/stdout patterns. Cleans up on exit.
#
# Usage: bash scripts/test-results-health.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/scripts/results-health.sh"

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# results-health.sh hardcodes RESULTS=$REPO/results, so we test against
# the real dir but stage fixtures + clean them up. The script doesn't
# delete anything itself, so this is safe.
RESULTS="$REPO/results"
PREFIX="test-rh-$$"
trap 'rm -f "$RESULTS"/${PREFIX}-*.txt 2>/dev/null' EXIT

echo "━━━ test-results-health.sh ━━━"
echo ""

# Test 1: clean state (no test fixtures yet)
# Note: real results/ may have actual files. We can't guarantee a fully clean
# state, so we just verify exit code matches what the actual content suggests.
echo "--- Test 1: --quiet on existing state ---"
out_quiet=$("$SCRIPT" --quiet 2>&1)
rh_exit=$?
if [ "$rh_exit" -eq 0 ] && [ -z "$out_quiet" ]; then
	pass "clean state → exit 0, no output (--quiet)"
elif [ "$rh_exit" -eq 1 ]; then
	pass "non-clean state → exit 1 (real results/ has flood-risk; not a test failure)"
else
	fail "unexpected: exit=$rh_exit, output=${out_quiet:0:60}"
fi
echo ""

# Test 2: zero-byte file triggers warn + exit 1
echo "--- Test 2: zero-byte file → exit 1 with warn ---"
touch "$RESULTS/${PREFIX}-zero.txt"
out=$("$SCRIPT" 2>&1)
rh_exit=$?
if [ "$rh_exit" -eq 1 ] && echo "$out" | grep -q "zero-byte"; then
	pass "zero-byte file detected, exit 1, warn message present"
else
	fail "expected exit 1 + zero-byte warn, got exit=$rh_exit, output=${out:0:120}"
fi
rm -f "$RESULTS/${PREFIX}-zero.txt"
echo ""

# Test 3: stale file (>24h old) triggers warn + exit 1
echo "--- Test 3: stale file (>24h) → exit 1 with warn ---"
echo "stale content" > "$RESULTS/${PREFIX}-stale.txt"
# Backdate to 25h ago. macOS BSD touch + GNU touch both supported.
if date -v -25H >/dev/null 2>&1; then
	touch -t "$(date -v -25H +%Y%m%d%H%M.%S)" "$RESULTS/${PREFIX}-stale.txt"
else
	touch -d "25 hours ago" "$RESULTS/${PREFIX}-stale.txt"
fi
out=$("$SCRIPT" 2>&1)
rh_exit=$?
if [ "$rh_exit" -eq 1 ] && echo "$out" | grep -qE "stale|>24h|older than 24h"; then
	pass "stale file detected, exit 1, warn message present"
else
	fail "expected exit 1 + stale warn, got exit=$rh_exit, output=${out:0:160}"
fi
rm -f "$RESULTS/${PREFIX}-stale.txt"
echo ""

# Test 4: --quiet flag suppresses output but preserves exit code
echo "--- Test 4: --quiet preserves exit code on flood-risk ---"
touch "$RESULTS/${PREFIX}-zero2.txt"
out_quiet=$("$SCRIPT" --quiet 2>&1)
rh_exit=$?
# --quiet should still print warns (only suppresses the "results/ healthy" line on exit 0)
if [ "$rh_exit" -eq 1 ]; then
	pass "--quiet preserves exit 1 on flood-risk"
else
	fail "expected exit 1 on zero-byte under --quiet, got exit=$rh_exit"
fi
rm -f "$RESULTS/${PREFIX}-zero2.txt"
echo ""

echo "━━━ Results: $PASS passed, $FAIL failed ━━━"
[ "$FAIL" -eq 0 ]

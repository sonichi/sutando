#!/usr/bin/env bash
# Smoke tests for gather.sh.
# Validates: expected output files exist, are non-empty when source exists,
# valid windows parse cleanly, invalid windows reject.

set -euo pipefail
cd "$(dirname "$0")/../../.."

PASS=0
FAIL=0
pass() { echo "  ✅ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ $1"; FAIL=$((FAIL + 1)); }

echo "━━━ gather.sh smoke tests ━━━"

# Test 1: valid window → non-zero exit + output dir
if OUT=$(bash skills/self-diagnose/scripts/gather.sh 24h 2>/dev/null); then
	[ -d "$OUT" ] && pass "24h window: output dir created at $OUT" || fail "24h window: no output dir"
else
	fail "24h window: gather exited non-zero"
	OUT=""
fi

# Test 2: expected files present
if [ -n "$OUT" ]; then
	for f in meta.txt git-log.txt git-status.txt build_log-tail.md pending-questions.md health.txt quota.txt; do
		[ -f "$OUT/$f" ] && pass "expected file exists: $f" || fail "missing file: $f"
	done
fi

# Test 3: meta.txt contains window + repo
if [ -n "$OUT" ] && [ -f "$OUT/meta.txt" ]; then
	grep -q "window:" "$OUT/meta.txt" && pass "meta.txt has window line" || fail "meta.txt missing window line"
	grep -q "repo:" "$OUT/meta.txt" && pass "meta.txt has repo line" || fail "meta.txt missing repo line"
fi

# Test 4: git-log non-empty when commits exist in window
if [ -n "$OUT" ] && [ -f "$OUT/git-log.txt" ]; then
	if git log --since="24 hours ago" --oneline | head -1 >/dev/null; then
		[ -s "$OUT/git-log.txt" ] && pass "git-log.txt non-empty (commits exist in window)" || fail "git-log.txt empty despite commits in window"
	else
		pass "git-log.txt: no commits in window, expected empty (skip non-empty check)"
	fi
fi

# Test 5: stdout last line equals output dir path
if [ -n "$OUT" ]; then
	[ -d "$OUT" ] && pass "stdout returns valid path" || fail "stdout path invalid"
fi

# Test 6: invalid window → non-zero exit
if bash skills/self-diagnose/scripts/gather.sh invalid-window 2>/dev/null 1>/dev/null; then
	fail "invalid window: gather should have rejected but exited 0"
else
	pass "invalid window: gather correctly rejected"
fi

# Test 7: 3d window format works
if OUT3=$(bash skills/self-diagnose/scripts/gather.sh 3d 2>/dev/null); then
	[ -d "$OUT3" ] && pass "3d window: output dir created" || fail "3d window: no output dir"
	rm -rf "$OUT3" 2>/dev/null
else
	fail "3d window: gather exited non-zero"
fi

# Test 8: results-recent-paths captures files mtime-newer than SINCE_EPOCH.
# This catches the "-newer meta.txt" bug that made this file empty on every run.
# Touch a fake result file to 1 hour old, then gather with 24h window.
mkdir -p results 2>/dev/null
FAKE_RESULT="results/sutando-test-recent-$(date +%s).txt"
echo "fake" > "$FAKE_RESULT"
# Backdate to 1 hour ago (BSD touch on macOS, GNU touch on linux — support both)
if date -v -1H >/dev/null 2>&1; then
	touch -t "$(date -v -1H +%Y%m%d%H%M.%S)" "$FAKE_RESULT"
else
	touch -d "1 hour ago" "$FAKE_RESULT"
fi
if OUT8=$(bash skills/self-diagnose/scripts/gather.sh 24h 2>/dev/null); then
	if grep -q "sutando-test-recent" "$OUT8/results-recent-paths.txt" 2>/dev/null; then
		pass "results-recent-paths captures 1h-old file in 24h window"
	else
		fail "results-recent-paths empty despite 1h-old file (regression of -newer meta.txt bug)"
	fi
	rm -rf "$OUT8" 2>/dev/null
fi
rm -f "$FAKE_RESULT" 2>/dev/null

# Cleanup
[ -n "${OUT:-}" ] && rm -rf "$OUT" 2>/dev/null || true

echo ""
echo "━━━ Results: $PASS passed, $FAIL failed ━━━"
[ $FAIL -eq 0 ] || exit 1

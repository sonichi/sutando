#!/usr/bin/env bash
# Smoke tests for gather.sh.
# Validates: expected output files exist, are non-empty when source exists,
# valid windows parse cleanly, invalid windows reject.

set -euo pipefail
cd "$(dirname "$0")/../../.."

PASS=0
FAIL=0
# Resolve REPO/WS exactly as gather.sh does, including its SUTANDO_ROOT-first
# precedence (gather.sh:19-20). Defined once and shared by every assertion that
# needs to know where gather will look — resolving differently in two places is
# how the results fixture and the collector silently disagreed.
_TG_REPO="${SUTANDO_ROOT:-$PWD}"
[ -f "$_TG_REPO/CLAUDE.md" ] || _TG_REPO="$PWD"
_TG_WS="$(bash "$_TG_REPO/scripts/sutando-config.sh" workspace)"
_TG_HOST="$(bash "$_TG_REPO/scripts/sutando-config.sh" host-label 2>/dev/null || echo unknown)"

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
	for f in meta.txt git-log.txt git-status.txt build_log-tail.md health.txt quota.txt; do
		[ -f "$OUT/$f" ] && pass "expected file exists: $f" || fail "missing file: $f"
	done
	# pending-questions.md is OPTIONAL: gather.sh:140-143 probes hosts/<host>/,
	# then the workspace root, then the repo root, and `cp … || true` produces
	# nothing when all three are absent. Requiring it unconditionally made this
	# suite non-hermetic — green only on a host that happens to have the file,
	# red in a clean worktree for behaviour the collector got right. The header
	# already states the contract: "non-empty when source exists". Asserted BOTH
	# ways so a genuinely missed copy still fails.
	_pq_src=""
	for _c in "$_TG_WS/hosts/$_TG_HOST/pending-questions.md" \
	          "$_TG_WS/pending-questions.md" \
	          "$_TG_REPO/pending-questions.md"; do
		[ -f "$_c" ] && { _pq_src="$_c"; break; }
	done
	if [ -n "$_pq_src" ]; then
		[ -f "$OUT/pending-questions.md" ] \
			&& pass "pending-questions.md copied (source: ${_pq_src#"$_TG_WS/"})" \
			|| fail "pending-questions.md MISSING despite a source at $_pq_src"
	else
		[ -f "$OUT/pending-questions.md" ] \
			&& fail "pending-questions.md present but NO source exists — where did it come from?" \
			|| pass "pending-questions.md absent, correctly: no source in any of the 3 probed locations"
	fi
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
# The fixture must live where gather.sh actually LOOKS. results/ is
# workspace-owned, so a fixture under the repo made this assertion test the
# retired layout — it passed only while gather.sh was reading the wrong place,
# and started failing the moment gather.sh was corrected.
# Resolve the workspace the SAME WAY gather.sh does, including its
# SUTANDO_ROOT-first precedence (gather.sh:19-20). Resolving cwd-relative here
# instead silently points the fixture at a different checkout's workspace than
# the one gather reads — which is exactly what happens when this test runs from
# a git worktree while SUTANDO_ROOT names the primary checkout.
_WS_FOR_FIXTURE="$_TG_WS"
mkdir -p "$_WS_FOR_FIXTURE/results" 2>/dev/null
FAKE_RESULT="$_WS_FOR_FIXTURE/results/sutando-test-recent-$(date +%s).txt"
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

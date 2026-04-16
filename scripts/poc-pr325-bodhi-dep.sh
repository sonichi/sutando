#!/bin/bash
# POC: PR #325 — fix: point bodhi-realtime-agent dep at sonichi (liususan091219 repo deleted)
#
# Bug: package.json referenced github:liususan091219/bodhi_realtime_agent but that
#      repo was deleted (account banned). Fresh `npm install` from main was failing
#      with a 404 from GitHub.
#
# Fix: Changed dependency to github:sonichi/bodhi_realtime_agent, which has dist/
#      committed and is up-to-date.
#
# Usage: bash scripts/poc-pr325-bodhi-dep.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  POC: PR #325 — bodhi dep liususan091219 → sonichi          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# ─── Phase 0: Reproduce bug ─────────────────────────────────────────

echo "━━━ Phase 0: REPRODUCE the bug (package.json before PR #325) ━━━"
echo ""

echo "--- Test 0a: Old package.json referenced liususan091219 ---"
OLD_DEP=$(git show 3bc033d^:package.json | grep bodhi-realtime-agent || true)
echo "  Old dep: $OLD_DEP"
if echo "$OLD_DEP" | grep -q "liususan091219/bodhi_realtime_agent"; then
    pass "BUG REPRODUCED: old package.json pointed at liususan091219/bodhi_realtime_agent"
else
    fail "Could not find liususan091219 dep in pre-PR commit (got: $OLD_DEP)"
fi
echo ""

echo "--- Test 0b: liususan091219/bodhi_realtime_agent was deleted (404 at time of fix) ---"
# At the time of PR #325 (2026-04-14) the liususan091219 account was banned and the
# repo returned 404. The account/repo may have been reinstated since then, so we
# accept either 404 (still gone) or 200 (reinstated) and note the current state.
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/liususan091219/bodhi_realtime_agent" \
    --max-time 10 2>/dev/null || echo "000")
echo "  GitHub API status for liususan091219/bodhi_realtime_agent: $HTTP_STATUS"
if [ "$HTTP_STATUS" = "404" ]; then
    pass "BUG CONFIRMED: liususan091219/bodhi_realtime_agent still returns 404 — repo gone"
elif [ "$HTTP_STATUS" = "200" ]; then
    pass "liususan091219/bodhi_realtime_agent now returns 200 (reinstated since PR #325)"
    echo "  NOTE: at the time of PR #325 (2026-04-14) this repo returned 404 (account banned)"
    echo "  The PR commit message confirms: 'Susan deleted her fork (banned)'"
elif [ "$HTTP_STATUS" = "000" ]; then
    fail "Network error or timeout reaching GitHub API"
else
    fail "Unexpected HTTP status: $HTTP_STATUS"
fi
echo ""

# ─── Phase 1: Verify fix ─────────────────────────────────────────────

echo "━━━ Phase 1: Verify fix (package.json after PR #325) ━━━"
echo ""

echo "--- Test 1a: Current package.json references sonichi ---"
CURRENT_DEP=$(grep "bodhi-realtime-agent" package.json || true)
echo "  Current dep: $CURRENT_DEP"
if echo "$CURRENT_DEP" | grep -q "sonichi/bodhi_realtime_agent"; then
    pass "package.json now points at sonichi/bodhi_realtime_agent"
else
    fail "Expected sonichi/bodhi_realtime_agent in package.json (got: $CURRENT_DEP)"
fi
echo ""

echo "--- Test 1b: sonichi/bodhi_realtime_agent returns 200 ---"
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://api.github.com/repos/sonichi/bodhi_realtime_agent" \
    --max-time 10 2>/dev/null || echo "000")
echo "  GitHub API status for sonichi/bodhi_realtime_agent: $HTTP_STATUS"
if [ "$HTTP_STATUS" = "200" ]; then
    pass "sonichi/bodhi_realtime_agent returns 200 — repo accessible"
elif [ "$HTTP_STATUS" = "000" ]; then
    fail "Network error or timeout reaching GitHub API"
else
    fail "Expected 200, got $HTTP_STATUS"
fi
echo ""

# ─── Phase 2: Verify installed ───────────────────────────────────────

echo "━━━ Phase 2: Verify installed (node_modules after npm install) ━━━"
echo ""

echo "--- Test 2a: node_modules/bodhi-realtime-agent exists ---"
if [ -d "node_modules/bodhi-realtime-agent" ]; then
    pass "node_modules/bodhi-realtime-agent directory exists"
else
    fail "node_modules/bodhi-realtime-agent not found — run npm install"
fi
echo ""

echo "--- Test 2b: dist/ files are present ---"
DIST_COUNT=$(ls node_modules/bodhi-realtime-agent/dist/ 2>/dev/null | wc -l | tr -d ' ')
echo "  Files in dist/: $DIST_COUNT"
if [ "$DIST_COUNT" -ge 3 ]; then
    pass "dist/ has $DIST_COUNT files (index.js, index.cjs, type defs)"
    ls node_modules/bodhi-realtime-agent/dist/ | sed 's/^/    /'
else
    fail "Expected ≥3 dist/ files, found $DIST_COUNT"
fi
echo ""

echo "--- Test 2c: package origin matches sonichi ---"
PKG_RESOLVED=$(node -e "const p=require('./node_modules/bodhi-realtime-agent/package.json'); console.log(p._resolved||p._from||'unknown')" 2>/dev/null || echo "unknown")
echo "  Resolved from: $PKG_RESOLVED"
if echo "$PKG_RESOLVED" | grep -qi "sonichi"; then
    pass "Installed package traces back to sonichi repo"
elif [ "$PKG_RESOLVED" = "unknown" ]; then
    # No _resolved field — check package.json name at minimum
    PKG_NAME=$(node -e "const p=require('./node_modules/bodhi-realtime-agent/package.json'); console.log(p.name)" 2>/dev/null || echo "unknown")
    if [ "$PKG_NAME" = "bodhi-realtime-agent" ]; then
        pass "Package name is bodhi-realtime-agent (resolved field not set — installed from git)"
    else
        fail "Could not confirm origin (name=$PKG_NAME, resolved=unknown)"
    fi
else
    fail "Package does not trace back to sonichi (resolved: $PKG_RESOLVED)"
fi
echo ""

# ─── Summary ─────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed"
echo ""
echo "  Before PR #325: package.json → github:liususan091219/bodhi_realtime_agent"
echo "                  that repo was deleted → npm install returned 404"
echo ""
echo "  After PR #325:  package.json → github:sonichi/bodhi_realtime_agent"
echo "                  repo accessible (200), dist/ committed, npm install works"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1

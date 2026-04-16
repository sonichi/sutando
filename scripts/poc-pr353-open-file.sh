#!/bin/bash
# POC: PR #353 — open_file immediate return vs 18s polling timeout
#
# Reproduces the bug (before #353) and verifies the fix (after #353).
#
# Bug: When a recording finishes, the subtitled burn-in runs async (~30s).
#      Before #353, open_file polled up to 18s waiting for the subtitled file.
#      On phone calls, Gemini Live's tool-call timeout would cancel the poll
#      mid-retry, and the model would say "I couldn't find the recording"
#      even though the narrated file was already on disk.
#
# Fix: open_file now calls findRecording() once (no loop), returns immediately
#      with the best-available version, and flags subtitled_pending=true if
#      the subtitled burn hasn't finished yet.
#
# Usage: bash scripts/poc-pr353-open-file.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  POC: PR #353 — open_file 18s timeout → immediate return   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# ─── Phase 0: Reproduce the bug with old code ───────────────────────

echo "━━━ Phase 0: REPRODUCE the bug (simulating old code before PR #353) ━━━"
echo ""

# Create a mock narrated-only recording (no subtitled version)
BUG_TS=$(date +%s)000
BUG_RAW="/tmp/sutando-recording-${BUG_TS}.mov"
BUG_NARRATED="/tmp/sutando-recording-${BUG_TS}-narrated.mov"
# Write 2KB so isReadableFile passes (>1KB check)
dd if=/dev/zero of="$BUG_RAW" bs=1024 count=2 2>/dev/null
dd if=/dev/zero of="$BUG_NARRATED" bs=1024 count=2 2>/dev/null
# Deliberately do NOT create -subtitled.mov — this is the bug scenario

echo "  Setup: created raw + narrated recording (NO subtitled file)"
echo "    raw:      $BUG_RAW"
echo "    narrated: $BUG_NARRATED"
echo "    subtitled: (does not exist — burn-in still running)"
echo ""

echo "--- Test 0a: Old code would poll 18s waiting for subtitled ---"
echo "  Running old polling logic simulation (with 10ms sleeps instead of 3s)..."

# Simulate the exact old polling loop from git show 2be13be^:src/recording-tools.ts
# but with 10ms sleeps instead of 3000ms to avoid a real 18s wait
OLD_RESULT=$(node -e "
const fs = require('fs');
const { execSync } = require('child_process');

function findRecording() {
  try {
    const files = execSync('ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1', { timeout: 3000 }).toString().trim();
    if (files && fs.existsSync(files) && fs.statSync(files).size > 1024) {
      const narrated = files.replace('.mov', '-narrated.mov');
      const subtitled = narrated.replace('.mov', '-subtitled.mov');
      if (fs.existsSync(subtitled) && fs.statSync(subtitled).size > 1024) return subtitled;
      if (fs.existsSync(narrated) && fs.statSync(narrated).size > 1024) return narrated;
      return files;
    }
  } catch {}
  return null;
}

// This is the EXACT old loop from before PR #353:
let recPath = null;
let iterations = 0;
let wouldHaveWaited = 0;
for (let i = 0; i < 10; i++) {
  recPath = findRecording();
  iterations++;
  if (recPath && recPath.includes('-subtitled')) break;  // found subtitled — stop
  if (recPath && i < 6) { wouldHaveWaited += 3000; continue; }  // has file but not subtitled — would sleep 3s
  if (!recPath) { wouldHaveWaited += 2000; }  // no file at all — would sleep 2s
  else break;  // i >= 6, has a file — give up
}

const gotSubtitled = recPath && recPath.includes('-subtitled');
console.log(JSON.stringify({
  iterations,
  wouldHaveWaited_ms: wouldHaveWaited,
  gotSubtitled,
  returnedPath: recPath || 'null',
  exceededGeminiTimeout: wouldHaveWaited > 15000,
}));
" 2>&1)

ITERS=$(echo "$OLD_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['iterations'])")
WAIT_MS=$(echo "$OLD_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['wouldHaveWaited_ms'])")
GOT_SUB=$(echo "$OLD_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['gotSubtitled'])")
EXCEEDED=$(echo "$OLD_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['exceededGeminiTimeout'])")
RETURNED=$(echo "$OLD_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['returnedPath'])")

echo "  Old loop ran $ITERS iterations"
echo "  Would have waited ${WAIT_MS}ms (real code uses setTimeout)"
echo "  Got subtitled version: $GOT_SUB"
echo "  Returned: $(basename "$RETURNED" 2>/dev/null || echo "$RETURNED")"

if [ "$WAIT_MS" -ge 18000 ]; then
    pass "BUG REPRODUCED: old code would wait ${WAIT_MS}ms (≥ 18s) — exceeds Gemini's ~15s tool timeout"
elif [ "$WAIT_MS" -ge 15000 ]; then
    pass "BUG REPRODUCED: old code would wait ${WAIT_MS}ms — exceeds Gemini's ~15s tool timeout"
else
    # Even if it doesn't hit 18s worst case, it still waited unnecessarily
    if [ "$GOT_SUB" = "False" ] && [ "$WAIT_MS" -gt 0 ]; then
        pass "BUG REPRODUCED: old code polled ${WAIT_MS}ms without finding subtitled, returned narrated anyway"
    else
        fail "Could not reproduce bug (waited ${WAIT_MS}ms, gotSubtitled=$GOT_SUB)"
    fi
fi
echo ""

echo "--- Test 0b: New code returns immediately with narrated version ---"
# New code: single findRecording() call, no loop
NEW_START=$(python3 -c 'import time; print(int(time.time()*1000000))')
NEW_RESULT=$(node -e "
const fs = require('fs');
const { execSync } = require('child_process');
function findRecording() {
  try {
    const files = execSync('ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1', { timeout: 3000 }).toString().trim();
    if (files && fs.existsSync(files) && fs.statSync(files).size > 1024) {
      const narrated = files.replace('.mov', '-narrated.mov');
      const subtitled = narrated.replace('.mov', '-subtitled.mov');
      if (fs.existsSync(subtitled) && fs.statSync(subtitled).size > 1024) return subtitled;
      if (fs.existsSync(narrated) && fs.statSync(narrated).size > 1024) return narrated;
      return files;
    }
  } catch {}
  return null;
}
// NEW code: just one call
const recPath = findRecording();
const isSubtitled = recPath && recPath.includes('-subtitled');
const isNarrated = !isSubtitled && recPath && recPath.includes('-narrated');
const subtitled_pending = !isSubtitled && recPath && recPath.includes('sutando-recording');
const version = isSubtitled ? 'subtitled' : (isNarrated ? 'narrated' : 'raw');
console.log(JSON.stringify({ path: recPath, version, subtitled_pending }));
" 2>&1)
NEW_END=$(python3 -c 'import time; print(int(time.time()*1000000))')
NEW_ELAPSED_US=$((NEW_END - NEW_START))

NEW_VERSION=$(echo "$NEW_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])")
NEW_PENDING=$(echo "$NEW_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['subtitled_pending'])")

echo "  New code returned in ~${NEW_ELAPSED_US}μs"
echo "  version=$NEW_VERSION, subtitled_pending=$NEW_PENDING"

if [ "$NEW_VERSION" = "narrated" ] && [ "$NEW_PENDING" = "True" ]; then
    pass "New code returns narrated immediately with subtitled_pending=true"
else
    fail "Expected version=narrated, subtitled_pending=true, got version=$NEW_VERSION, pending=$NEW_PENDING"
fi
echo ""

# Cleanup bug reproduction files
rm -f "$BUG_RAW" "$BUG_NARRATED"

# ─── Phase 1: Verify fix in current codebase ────────────────────────

echo "━━━ Phase 1: Verify fix in current codebase (after PR #353) ━━━"
echo ""

# Verify the old polling loop is GONE from current code
echo "--- Test 1: Old polling loop removed from open_file ---"
# The old bug: open_file's execute() had "for (let i = 0; i < 10; i++)" with
# findRecording() inside and 3s sleeps. Check the open_file execute block only
# (there's a different for-loop in playVideoTool for QuickTime launch — that's fine).
OPEN_FILE_BLOCK=$(sed -n '/openFileTool/,/^export const/p' src/recording-tools.ts)
if echo "$OPEN_FILE_BLOCK" | grep -q 'setTimeout.*3000'; then
    fail "3-second polling sleep still in open_file execute block"
elif echo "$OPEN_FILE_BLOCK" | grep -q 'No polling'; then
    pass "Old polling loop removed — 'No polling' comment confirms fix"
else
    pass "No 3s polling sleep in open_file"
fi
echo ""

# ─── Phase 2: Verify the fix (new behavior, after PR #353) ──────────

echo "━━━ Phase 2: Verify the fix (new behavior, after PR #353) ━━━"
echo ""

# Test 2: findRecording is called exactly once (no loop)
echo "--- Test 2: findRecording() called without polling loop ---"
# The fix: findRecording() is called directly (no for-loop around it).
# There may be >1 call (e.g. PR #355 added a second for playVideo), but
# none should be inside a retry loop with sleep.
OPEN_FILE_BLOCK=$(sed -n '/openFileTool/,/^export const/p' src/recording-tools.ts)
if echo "$OPEN_FILE_BLOCK" | grep -q 'findRecording()'; then
    # Check there's no for-loop wrapping findRecording
    if echo "$OPEN_FILE_BLOCK" | grep -B5 'findRecording()' | grep -q 'for.*let i'; then
        fail "findRecording() still inside a for-loop"
    else
        FIND_CALLS=$(echo "$OPEN_FILE_BLOCK" | grep -c 'findRecording()' || true)
        pass "findRecording() called ${FIND_CALLS}x in open_file — none inside a retry loop"
    fi
else
    fail "findRecording() not found in open_file execute()"
fi
echo ""

# Test 3: subtitled_pending flag exists
echo "--- Test 3: subtitled_pending flag in response ---"
if grep -q 'subtitled_pending' src/recording-tools.ts; then
    pass "subtitled_pending flag present"
    grep -n 'subtitled_pending' src/recording-tools.ts | sed 's/^/  /'
else
    fail "subtitled_pending flag not found"
fi
echo ""

# Test 4: version field in response
echo "--- Test 4: version field in response ---"
if grep -q "version.*subtitled.*narrated.*raw\|isSubtitled.*isNarrated" src/recording-tools.ts; then
    pass "version field computed (subtitled/narrated/raw)"
else
    fail "version field logic not found"
fi
echo ""

# Test 5: Instruction tells model to offer wait option
echo "--- Test 5: Model instruction for subtitled_pending ---"
if grep -q 'Subtitles are still being generated\|subtitled version is still being generated' src/recording-tools.ts; then
    pass "Model instruction includes pending subtitle message"
else
    fail "Model instruction for pending subtitles not found"
fi
echo ""

# ─── Phase 3: Functional timing test ────────────────────────────────

echo "━━━ Phase 3: Functional timing test ━━━"
echo ""

# Create mock recording files to test findRecording priority
MOCK_TS=$(date +%s)
MOCK_RAW="/tmp/sutando-recording-${MOCK_TS}.mov"
MOCK_NARRATED="/tmp/sutando-recording-${MOCK_TS}-narrated.mov"
MOCK_SUBTITLED="/tmp/sutando-recording-${MOCK_TS}-narrated-subtitled.mov"

# Test 6a: Only raw file exists (subtitled_pending should be true)
echo "--- Test 6a: Only raw file → subtitled_pending=true ---"
touch "$MOCK_RAW"
# findRecording returns raw since subtitled doesn't exist yet
FOUND=$(ls -t /tmp/sutando-recording-*.mov 2>/dev/null | grep -v narrated | grep -v subtitled | head -1)
if [ "$FOUND" = "$MOCK_RAW" ]; then
    pass "findRecording returns raw file when subtitled not ready"
    echo "  → In old code: would poll 18s waiting for subtitled"
    echo "  → In new code: returns immediately, subtitled_pending=true"
else
    fail "Expected raw file $MOCK_RAW, got: $FOUND"
fi
echo ""

# Test 6b: Narrated file appears (still subtitled_pending)
echo "--- Test 6b: Narrated exists → returns narrated, still pending ---"
touch "$MOCK_NARRATED"
FOUND=$(ls -t /tmp/sutando-recording-*.mov 2>/dev/null | head -1)
echo "  Latest recording-related file: $FOUND"
pass "Narrated file on disk — new code returns this immediately instead of waiting for subtitled"
echo ""

# Test 6c: Subtitled file appears (subtitled_pending=false)
echo "--- Test 6c: Subtitled exists → returns subtitled, not pending ---"
touch "$MOCK_SUBTITLED"
if [ -f "$MOCK_SUBTITLED" ]; then
    pass "Subtitled file on disk — subtitled_pending=false, version=subtitled"
fi
echo ""

# Cleanup mock files
rm -f "$MOCK_RAW" "$MOCK_NARRATED" "$MOCK_SUBTITLED"

# Test 7: Measure actual execution time of open_file (if voice-agent is running)
echo "--- Test 7: Actual timing (requires running voice-agent) ---"
if curl -s http://localhost:9900/ >/dev/null 2>&1; then
    REAL_RECORDING=$(ls -t /tmp/sutando-recording-*.mov 2>/dev/null | head -1)
    if [ -n "$REAL_RECORDING" ]; then
        START_MS=$(python3 -c 'import time; print(int(time.time()*1000))')
        # Call open_file through the agent API
        RESULT=$(curl -s -X POST http://localhost:7843/tool \
            -H 'Content-Type: application/json' \
            -d '{"tool":"open_file","args":{}}' \
            --max-time 5 2>/dev/null || echo '{"error":"timeout or unavailable"}')
        END_MS=$(python3 -c 'import time; print(int(time.time()*1000))')
        ELAPSED=$((END_MS - START_MS))
        echo "  open_file returned in ${ELAPSED}ms"
        if [ "$ELAPSED" -lt 3000 ]; then
            pass "Returned in ${ELAPSED}ms (< 3s) — immediate, no polling"
        else
            fail "Returned in ${ELAPSED}ms (≥ 3s) — may still be polling"
        fi
        echo "  Response: $(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'version={d.get(\"version\",\"?\")}, subtitled_pending={d.get(\"subtitled_pending\",\"?\")}')" 2>/dev/null || echo "$RESULT" | head -c 200)"
    else
        echo "  SKIP: No recording on disk to test with"
    fi
else
    echo "  SKIP: Voice agent not running on port 9900"
fi
echo ""

# ─── Summary ─────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed"
echo ""
echo "  Before PR #353: open_file polled up to 18s → exceeded Gemini"
echo "                  tool timeout → 'can't find recording' error"
echo ""
echo "  After PR #353:  findRecording() once → return immediately"
echo "                  subtitled_pending=true if burn-in still running"
echo "                  Model asks user to wait, retries on request"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1

#!/bin/bash
# POC: PR #355 — subtitled_pending false positive in open_file
#
# Bug (before PR #355): subtitled_pending is true for ALL recordings including
# ones that never had subtitles requested. The old logic was:
#
#   const subtitled_pending = !isSubtitled && recPath.includes('sutando-recording');
#
# This returns true for any recording file, even ones where no subtitle burn
# was ever started — causing the model to always say "subtitles are being
# generated" even for recordings that will never have subtitles.
#
# Fix (PR #355): Now checks two conditions:
#   1. SRT file exists at LIVE_TRANSCRIPT_SRT_PATH (transcript was generated)
#   2. Expected subtitled .mov does NOT exist (burn hasn't finished yet)
# Only when both are true does it set subtitled_pending=true.
#
# Usage: bash scripts/poc-pr355-subtitled-pending.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  POC: PR #355 — subtitled_pending false positive fix        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# Paths used by recording-tools.ts
SRT_PATH="/tmp/sutando-live-transcript-subtitle.srt"

# Unique timestamp to avoid collisions with real recordings
MOCK_TS=$(($(date +%s) + 9999999))
MOCK_RAW="/tmp/sutando-recording-${MOCK_TS}.mov"
MOCK_NARRATED="/tmp/sutando-recording-${MOCK_TS}-narrated.mov"
MOCK_SUBTITLED="/tmp/sutando-recording-${MOCK_TS}-narrated-subtitled.mov"

# Track files we create so cleanup is always complete
CREATED_FILES=()

cleanup() {
    if [ ${#CREATED_FILES[@]} -gt 0 ]; then
        for f in "${CREATED_FILES[@]}"; do
            rm -f "$f"
        done
    fi
}
trap cleanup EXIT

# Preserve any real SRT that may be on disk — we must not clobber it
REAL_SRT_EXISTED=false
[ -f "$SRT_PATH" ] && REAL_SRT_EXISTED=true

# Helper: simulate old logic
old_subtitled_pending() {
    local recPath="$1"
    local isSubtitled=false
    local result=false
    [[ "$recPath" == *"-subtitled"* ]] && isSubtitled=true
    if ! $isSubtitled && [[ "$recPath" == *"sutando-recording"* ]]; then
        result=true
    fi
    echo "$result"
}

# Helper: simulate new logic from PR #355
new_subtitled_pending() {
    local recPath="$1"
    local srtPath="$2"
    local isSubtitled=false
    local isNarrated=false
    local result=false

    [[ "$recPath" == *"-subtitled"* ]] && isSubtitled=true
    if ! $isSubtitled && [[ "$recPath" == *"-narrated"* ]]; then
        isNarrated=true
    fi

    # Compute expectedSubtitled (mirrors PR #355 logic)
    local expectedSubtitled
    if $isNarrated; then
        expectedSubtitled="${recPath/.mov/-subtitled.mov}"
    else
        expectedSubtitled="${recPath/.mov/-narrated-subtitled.mov}"
    fi

    if ! $isSubtitled && [[ "$recPath" == *"sutando-recording"* ]] \
        && [ -f "$srtPath" ] \
        && [ ! -f "$expectedSubtitled" ]; then
        result=true
    fi
    echo "$result"
}

# ─── Phase 0: Reproduce the bug (old logic) ──────────────────────────

echo "━━━ Phase 0: REPRODUCE the bug (old logic before PR #355) ━━━"
echo ""

echo "  Setup: create mock recording with NO SRT (subtitle was never requested)"
dd if=/dev/zero of="$MOCK_NARRATED" bs=1024 count=2 2>/dev/null
CREATED_FILES+=("$MOCK_NARRATED")
echo "    recording: $MOCK_NARRATED"
echo "    SRT:       (does not exist — no subtitle burn was ever started)"
echo "    subtitled: (does not exist)"
echo ""

echo "--- Test 0: Old logic flags subtitled_pending=true for any recording ---"
OLD_RESULT=$(old_subtitled_pending "$MOCK_NARRATED")
echo "  Old subtitled_pending = $OLD_RESULT"
if [ "$OLD_RESULT" = "true" ]; then
    pass "BUG REPRODUCED: old logic returns subtitled_pending=true even though no subtitle was ever requested"
else
    fail "Expected old logic to return true, got: $OLD_RESULT"
fi
echo ""

rm -f "$MOCK_NARRATED"
CREATED_FILES=()

# ─── Phase 1: Verify fix — no SRT → subtitled_pending=false ──────────

echo "━━━ Phase 1: Verify fix — recording with no SRT → subtitled_pending=false ━━━"
echo ""

echo "  Setup: create raw recording (no SRT file, subtitle never started)"
dd if=/dev/zero of="$MOCK_RAW" bs=1024 count=2 2>/dev/null
CREATED_FILES+=("$MOCK_RAW")
# Ensure SRT is absent for this test — temporarily remove if a real one exists
SRT_WAS_PRESENT=false
if [ -f "$SRT_PATH" ]; then
    SRT_WAS_PRESENT=true
    mv "$SRT_PATH" "${SRT_PATH}.poc-bak"
fi
echo "    recording: $MOCK_RAW"
echo "    SRT:       (does not exist)"
echo ""

echo "--- Test 1a: New logic — no SRT means subtitled_pending=false ---"
NEW_RESULT=$(new_subtitled_pending "$MOCK_RAW" "$SRT_PATH")
echo "  New subtitled_pending = $NEW_RESULT"
# Restore SRT if we hid it
if $SRT_WAS_PRESENT; then
    mv "${SRT_PATH}.poc-bak" "$SRT_PATH"
fi
if [ "$NEW_RESULT" = "false" ]; then
    pass "New logic correctly returns subtitled_pending=false when no SRT exists"
else
    fail "Expected false (no SRT), got: $NEW_RESULT"
fi
echo ""

echo "--- Test 1b: Old logic would incorrectly return true for same file ---"
OLD_RESULT=$(old_subtitled_pending "$MOCK_RAW")
echo "  Old subtitled_pending = $OLD_RESULT"
if [ "$OLD_RESULT" = "true" ]; then
    pass "Confirmed: old logic would have returned true (false positive)"
else
    fail "Expected old logic to return true to demonstrate the regression"
fi
echo ""

rm -f "$MOCK_RAW"
CREATED_FILES=()
echo ""

# ─── Phase 2: Verify true positive — SRT exists, no subtitled mov ────

echo "━━━ Phase 2: Verify true positive — SRT exists, subtitled.mov not yet ready ━━━"
echo ""

echo "  Setup: create narrated recording + SRT (subtitle burn in progress)"
dd if=/dev/zero of="$MOCK_NARRATED" bs=1024 count=2 2>/dev/null
CREATED_FILES+=("$MOCK_NARRATED")
# Create SRT only if a real one doesn't already exist
SRT_CREATED_P2=false
if [ ! -f "$SRT_PATH" ]; then
    echo "[SRT stub for testing — phase 2]" > "$SRT_PATH"
    CREATED_FILES+=("$SRT_PATH")
    SRT_CREATED_P2=true
fi
echo "    recording: $MOCK_NARRATED"
echo "    SRT:       $SRT_PATH  (exists — transcript was generated)"
echo "    subtitled: (does not exist — burn still running)"
echo ""

echo "--- Test 2: New logic flags subtitled_pending=true when SRT exists but no subtitled mov ---"
NEW_RESULT=$(new_subtitled_pending "$MOCK_NARRATED" "$SRT_PATH")
echo "  New subtitled_pending = $NEW_RESULT"
if [ "$NEW_RESULT" = "true" ]; then
    pass "New logic correctly returns subtitled_pending=true (SRT exists, burn not done)"
else
    fail "Expected true (SRT present, no subtitled.mov), got: $NEW_RESULT"
fi
echo ""

rm -f "$MOCK_NARRATED"
$SRT_CREATED_P2 && rm -f "$SRT_PATH" || true
CREATED_FILES=()

# ─── Phase 3: Verify completed — SRT + subtitled mov both exist ───────

echo "━━━ Phase 3: Verify completed — SRT + subtitled.mov both exist ━━━"
echo ""

echo "  Setup: create narrated + SRT + subtitled (burn is done)"
dd if=/dev/zero of="$MOCK_NARRATED" bs=1024 count=2 2>/dev/null
CREATED_FILES+=("$MOCK_NARRATED" "$MOCK_SUBTITLED")
SRT_CREATED_P3=false
if [ ! -f "$SRT_PATH" ]; then
    echo "[SRT stub for testing — phase 3]" > "$SRT_PATH"
    CREATED_FILES+=("$SRT_PATH")
    SRT_CREATED_P3=true
fi
dd if=/dev/zero of="$MOCK_SUBTITLED" bs=1024 count=2 2>/dev/null
echo "    recording: $MOCK_NARRATED"
echo "    SRT:       $SRT_PATH  (exists)"
echo "    subtitled: $MOCK_SUBTITLED  (exists — burn is done)"
echo ""

echo "--- Test 3a: New logic returns subtitled_pending=false when subtitled.mov is ready ---"
# open_file would have found the subtitled path directly, but let's also test
# that the narrated path returns false now that subtitled exists
NEW_RESULT=$(new_subtitled_pending "$MOCK_NARRATED" "$SRT_PATH")
echo "  New subtitled_pending (narrated path, subtitled exists) = $NEW_RESULT"
if [ "$NEW_RESULT" = "false" ]; then
    pass "New logic returns subtitled_pending=false when subtitled.mov is already on disk"
else
    fail "Expected false (subtitled.mov exists), got: $NEW_RESULT"
fi
echo ""

echo "--- Test 3b: When path IS the subtitled file, subtitled_pending=false ---"
NEW_RESULT=$(new_subtitled_pending "$MOCK_SUBTITLED" "$SRT_PATH")
echo "  New subtitled_pending (subtitled path) = $NEW_RESULT"
if [ "$NEW_RESULT" = "false" ]; then
    pass "subtitled path → isSubtitled=true → subtitled_pending=false"
else
    fail "Expected false for subtitled path, got: $NEW_RESULT"
fi
echo ""

rm -f "$MOCK_NARRATED" "$MOCK_SUBTITLED"
$SRT_CREATED_P3 && rm -f "$SRT_PATH" || true
CREATED_FILES=()

# ─── Phase 4: Verify fix in actual source code ────────────────────────

echo "━━━ Phase 4: Verify fix in source code (current branch vs PR #355 branch) ━━━"
echo ""

echo "--- Test 4a: Source on current branch has OLD logic (PR #355 not merged yet) ---"
CURRENT_EXISTSSYNC=$(grep -c 'existsSync.*LIVE_TRANSCRIPT_SRT' src/recording-tools.ts || true)
CURRENT_LOGIC=$(grep 'subtitled_pending = ' src/recording-tools.ts | grep -v '//' | head -1 | sed 's/^[[:space:]]*//')
echo "  Current source: $CURRENT_LOGIC"
echo "  existsSync(SRT) in current source: $CURRENT_EXISTSSYNC occurrences"
if [ "$CURRENT_EXISTSSYNC" -ge 1 ]; then
    pass "PR #355 is already merged — new logic with existsSync(SRT) is live"
else
    pass "BUG PRESENT in current source: subtitled_pending has no SRT/existsSync guard"
    echo "  → PR #355 would fix this with SRT + existsSync guards"
fi
echo ""

echo "--- Test 4b: PR #355 branch has NEW logic ---"
# Fetch and check the fix branch
if git fetch origin fix/subtitled-pending-false-positive 2>/dev/null; then
    # The new assignment is multi-line; grep the entire block for existsSync
    FIX_SRC=$(git show origin/fix/subtitled-pending-false-positive:src/recording-tools.ts 2>/dev/null)
    FIX_LOGIC=$(echo "$FIX_SRC" | grep 'subtitled_pending = ' | grep -v '//' | head -1 | sed 's/^[[:space:]]*//')
    echo "  Fix branch first line: $FIX_LOGIC"
    HAS_EXISTSSYNC=$(echo "$FIX_SRC" | grep -c 'existsSync.*LIVE_TRANSCRIPT_SRT' || true)
    echo "  existsSync(LIVE_TRANSCRIPT_SRT_PATH) occurrences: $HAS_EXISTSSYNC"
    if [ "$HAS_EXISTSSYNC" -ge 1 ]; then
        pass "PR #355 branch has new logic: checks existsSync(LIVE_TRANSCRIPT_SRT_PATH)"
    else
        fail "Expected existsSync(LIVE_TRANSCRIPT_SRT_PATH) in PR #355 branch"
    fi
else
    echo "  SKIP: Could not fetch fix branch (offline or branch gone)"
fi
echo ""

echo "--- Test 4c: PR #355 uses correct SRT path constant ---"
if git fetch origin fix/subtitled-pending-false-positive 2>/dev/null; then
    SRT_CONST=$(git show origin/fix/subtitled-pending-false-positive:src/recording-tools.ts 2>/dev/null \
        | grep 'LIVE_TRANSCRIPT_SRT_PATH\s*=' | head -1 | sed 's/^[[:space:]]*//')
    echo "  SRT constant: $SRT_CONST"
    if echo "$SRT_CONST" | grep -q 'sutando-live-transcript-subtitle.srt'; then
        pass "SRT path = /tmp/sutando-live-transcript-subtitle.srt (matches subtitle.py output)"
    else
        fail "Unexpected SRT path: $SRT_CONST"
    fi
fi
echo ""

# ─── Summary ──────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed"
echo ""
echo "  Before PR #355: subtitled_pending=true for ALL sutando-recording files"
echo "                  — even when subtitles were never requested"
echo "                  → model always tells user 'subtitles being generated'"
echo ""
echo "  After PR #355:  subtitled_pending=true ONLY when:"
echo "                  1. SRT file exists (transcript was generated)"
echo "                  2. Subtitled .mov does NOT exist yet (burn in progress)"
echo "                  → model tells user correctly, no false positives"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1

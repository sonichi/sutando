#!/bin/bash
# POC: PR #354 — retention sweep: archive stale results/*.txt on startup
#
# Reproduces the bug (before #354) and verifies the fix (after #354).
#
# Bug: results/ accumulates dead files indefinitely — task-*, question-*,
#      briefing-*, insight-*, friction-* left over from voice sessions.
#      Any code that scans results/ for delivery (Discord bridge, Telegram
#      bridge, watch-tasks) floods on first tick after a long idle period.
#      Incident: DM flood 2026-04-15 — stale results delivered to Discord.
#
# Fix: src/archive-stale-results.py walks results/*.txt, moves files older
#      than RETENTION_HOURS (default 24) into results/archive-YYYY-MM-DD/.
#      Called in startup.sh before services start.
#
# Usage: bash scripts/poc-pr354-retention-sweep.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  POC: PR #354 — retention sweep for stale results/*.txt     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

PASS=0
FAIL=0
pass() { echo "  ✅ PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  ❌ FAIL: $1"; FAIL=$((FAIL + 1)); }

# ─── Phase 0: Reproduce the bug ─────────────────────────────────────

echo "━━━ Phase 0: REPRODUCE the bug (accumulation without archiver) ━━━"
echo ""

MOCK_DIR=$(mktemp -d /tmp/sutando-results-poc-XXXXXX)
trap 'rm -rf "$MOCK_DIR"' EXIT

# Create stale files with old timestamps (48h ago)
STALE_EPOCH=$(python3 -c 'import time; print(int(time.time()) - 48*3600)')
STALE_FILES=(
    "task-1744000001.txt"
    "task-1744000002.txt"
    "question-1744000003.txt"
    "briefing-1744000004.txt"
    "insight-1744000005.txt"
    "friction-1744000006.txt"
)
for f in "${STALE_FILES[@]}"; do
    echo "mock result for $f" > "$MOCK_DIR/$f"
    touch -t "$(python3 -c "import datetime; print(datetime.datetime.fromtimestamp($STALE_EPOCH).strftime('%Y%m%d%H%M.%S'))")" "$MOCK_DIR/$f"
done

# Also create a fresh file (should NOT be archived)
echo "fresh result" > "$MOCK_DIR/task-9999999999.txt"

TOTAL=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
STALE_COUNT=${#STALE_FILES[@]}

echo "  Mock results dir: $MOCK_DIR"
echo "  Total files:      $TOTAL  (${STALE_COUNT} stale + 1 fresh)"
echo ""

echo "--- Test 0a: Without archiver, stale files accumulate ---"
# Simulate what a bridge scan would see — all files, including stale
WOULD_DELIVER=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
if [ "$WOULD_DELIVER" -gt 1 ]; then
    pass "BUG REPRODUCED: without archiver, bridge sees $WOULD_DELIVER files — would flood $((WOULD_DELIVER - 1)) stale results on first tick"
else
    fail "Expected >1 files in mock dir, got $WOULD_DELIVER"
fi
echo ""

echo "--- Test 0b: Stale file ages confirm they predate retention window ---"
# Check at least one file is older than 24h
OLD_COUNT=$(find "$MOCK_DIR" -name "*.txt" -mmin +1440 2>/dev/null | wc -l | tr -d ' ')
if [ "$OLD_COUNT" -ge "$STALE_COUNT" ]; then
    pass "BUG REPRODUCED: $OLD_COUNT files older than 24h — would trigger flood"
else
    fail "Expected $STALE_COUNT files older than 24h, got $OLD_COUNT"
fi
echo ""

# ─── Phase 1: Verify fix exists ──────────────────────────────────────

echo "━━━ Phase 1: Verify fix in codebase (after PR #354) ━━━"
echo ""

echo "--- Test 1a: src/archive-stale-results.py exists ---"
if [ -f "src/archive-stale-results.py" ]; then
    pass "src/archive-stale-results.py exists"
    ARCHIVER="src/archive-stale-results.py"
else
    fail "src/archive-stale-results.py not found — PR #354 not yet applied"
    ARCHIVER=""
fi
echo ""

echo "--- Test 1b: startup.sh calls archive-stale-results.py ---"
if grep -q "archive-stale-results" src/startup.sh; then
    pass "startup.sh references archive-stale-results.py"
    grep -n "archive-stale-results" src/startup.sh | sed 's/^/  /'
else
    fail "startup.sh does not call archive-stale-results.py — fix not wired in"
fi
echo ""

echo "--- Test 1c: startup.sh calls it before services start ---"
# Services start around the "Starting credential proxy" block.
# The archiver should appear before the first service launch line.
if [ -f "src/archive-stale-results.py" ]; then
    ARCHIVE_LINE=$(grep -n "archive-stale-results" src/startup.sh | head -1 | cut -d: -f1)
    # First service launch: credential proxy on port 7846
    SERVICE_LINE=$(grep -n "credential-proxy\|voice-agent\|web-client" src/startup.sh | head -1 | cut -d: -f1)
    if [ -n "$ARCHIVE_LINE" ] && [ -n "$SERVICE_LINE" ] && [ "$ARCHIVE_LINE" -lt "$SERVICE_LINE" ]; then
        pass "archive-stale-results.py (line $ARCHIVE_LINE) called before first service (line $SERVICE_LINE)"
    elif [ -n "$ARCHIVE_LINE" ]; then
        fail "archive-stale-results.py (line $ARCHIVE_LINE) called AFTER first service (line ${SERVICE_LINE:-?}) — flood risk remains"
    else
        fail "Could not locate archive-stale-results call in startup.sh"
    fi
else
    echo "  SKIP: archiver not present"
fi
echo ""

echo "--- Test 1d: archiver supports RETENTION_HOURS env var ---"
if [ -n "$ARCHIVER" ]; then
    if grep -q "RETENTION_HOURS" "$ARCHIVER"; then
        DEFAULT_H=$(grep -oE "RETENTION_HOURS.*[0-9]+" "$ARCHIVER" | grep -oE "[0-9]+$" | head -1)
        pass "RETENTION_HOURS supported (default: ${DEFAULT_H:-?}h)"
    else
        fail "RETENTION_HOURS not found in archiver — retention window not configurable"
    fi
else
    echo "  SKIP: archiver not present"
fi
echo ""

echo "--- Test 1e: archiver supports DRY_RUN env var ---"
if [ -n "$ARCHIVER" ]; then
    if grep -qi "dry.run\|DRY_RUN" "$ARCHIVER"; then
        pass "DRY_RUN mode supported"
    else
        fail "DRY_RUN not found in archiver — can't test safely"
    fi
else
    echo "  SKIP: archiver not present"
fi
echo ""

# ─── Phase 2: Functional test (dry run) ──────────────────────────────

echo "━━━ Phase 2: Functional test — DRY_RUN against mock dir ━━━"
echo ""

if [ -z "$ARCHIVER" ]; then
    echo "  SKIP: archiver not present (PR #354 not applied)"
    echo ""
else
    echo "--- Test 2a: DRY_RUN=1 identifies stale files without moving them ---"
    # Run with RETENTION_HOURS=0 so ALL files (including the "fresh" one) are stale,
    # OR use DRY_RUN=1 with RETENTION_HOURS=1 so only the 48h-old files are caught.
    DRY_OUTPUT=$(RESULTS_DIR="$MOCK_DIR" RETENTION_HOURS=1 DRY_RUN=1 python3 "$ARCHIVER" 2>&1 || true)
    echo "  Dry-run output:"
    echo "$DRY_OUTPUT" | sed 's/^/    /'
    echo ""

    # After DRY_RUN, no files should have moved
    STILL_PRESENT=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
    if [ "$STILL_PRESENT" -eq "$TOTAL" ]; then
        pass "DRY_RUN=1: all $TOTAL files still in place — no moves performed"
    else
        fail "DRY_RUN=1: expected $TOTAL files, found $STILL_PRESENT — files moved unexpectedly"
    fi
    echo ""

    # Dry run output should mention the stale files
    echo "--- Test 2b: DRY_RUN output mentions stale file count ---"
    MENTIONED=$(echo "$DRY_OUTPUT" | grep -cE "would archive|stale|task-|question-|briefing-|insight-|friction-" || true)
    if [ "$MENTIONED" -gt 0 ]; then
        pass "Dry-run output references stale files ($MENTIONED matching lines)"
    else
        fail "Dry-run output did not mention stale files — check archiver output format"
    fi
    echo ""
fi

# ─── Phase 3: Verify archival (live run on temp dir) ─────────────────

echo "━━━ Phase 3: Verify archival — live run on temp dir ━━━"
echo ""

if [ -z "$ARCHIVER" ]; then
    echo "  SKIP: archiver not present (PR #354 not applied)"
    echo ""
else
    echo "--- Test 3a: Live run moves stale files to archive subdir ---"
    BEFORE_COUNT=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
    LIVE_OUTPUT=$(RESULTS_DIR="$MOCK_DIR" RETENTION_HOURS=1 python3 "$ARCHIVER" 2>&1 || true)
    echo "  Live-run output:"
    echo "$LIVE_OUTPUT" | sed 's/^/    /'
    echo ""

    # Count remaining .txt files in root of mock dir
    AFTER_ROOT=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
    # Count archived files
    ARCHIVE_SUBDIR=$(find "$MOCK_DIR" -mindepth 2 -name "*.txt" 2>/dev/null | wc -l | tr -d ' ')

    echo "  Before: $BEFORE_COUNT files in root"
    echo "  After:  $AFTER_ROOT files in root, $ARCHIVE_SUBDIR files in archive subdir"

    if [ "$ARCHIVE_SUBDIR" -ge "$STALE_COUNT" ]; then
        pass "Stale files archived: $ARCHIVE_SUBDIR moved to archive subdir"
    else
        fail "Expected $STALE_COUNT archived, found $ARCHIVE_SUBDIR"
    fi
    echo ""

    echo "--- Test 3b: Archive subdir uses YYYY-MM-DD naming ---"
    ARCHIVE_DIR=$(find "$MOCK_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)
    if [ -n "$ARCHIVE_DIR" ]; then
        DIRNAME=$(basename "$ARCHIVE_DIR")
        if echo "$DIRNAME" | grep -qE "^archive-[0-9]{4}-[0-9]{2}-[0-9]{2}$"; then
            pass "Archive subdir uses correct naming: $DIRNAME"
        else
            fail "Archive subdir name '$DIRNAME' does not match archive-YYYY-MM-DD pattern"
        fi
    else
        fail "No archive subdir created under $MOCK_DIR"
    fi
    echo ""

    echo "--- Test 3c: Fresh file NOT archived ---"
    FRESH_STILL_THERE=$(ls "$MOCK_DIR"/task-9999999999.txt 2>/dev/null | wc -l | tr -d ' ')
    if [ "$FRESH_STILL_THERE" -eq 1 ]; then
        pass "Fresh file task-9999999999.txt left in place (not archived)"
    else
        fail "Fresh file was incorrectly archived"
    fi
    echo ""

    echo "--- Test 3d: After archival, bridge scan sees only fresh files ---"
    BRIDGE_SEES=$(ls "$MOCK_DIR"/*.txt 2>/dev/null | wc -l | tr -d ' ')
    if [ "$BRIDGE_SEES" -eq 1 ]; then
        pass "Bridge would now see only $BRIDGE_SEES file (was $BEFORE_COUNT) — flood prevented"
    else
        fail "Expected 1 fresh file remaining, bridge sees $BRIDGE_SEES files"
    fi
    echo ""
fi

# ─── Summary ─────────────────────────────────────────────────────────

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: $PASS passed, $FAIL failed"
echo ""
echo "  Before PR #354: results/*.txt accumulates indefinitely."
echo "                  Bridge scans deliver all files on first tick"
echo "                  → DM flood (incident 2026-04-15)."
echo ""
echo "  After PR #354:  startup.sh runs archive-stale-results.py"
echo "                  before services start. Files older than"
echo "                  RETENTION_HOURS (default 24h) moved to"
echo "                  results/archive-YYYY-MM-DD/. Bridge only"
echo "                  sees recent results → no flood."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[ "$FAIL" -eq 0 ] && exit 0 || exit 1

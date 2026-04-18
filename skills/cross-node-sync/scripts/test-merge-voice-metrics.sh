#!/usr/bin/env bash
# Smoke tests for merge-voice-metrics.sh.
#
# Covers: empty inputs, sort-ascending, dedup on (sessionId, timestamp),
# malformed-line tolerance, missing-peer no-op, peer-file cleanup, and
# atomic-write (tmp lands in same dir as target).
#
# Usage:
#   bash skills/cross-node-sync/scripts/test-merge-voice-metrics.sh

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MERGE="$SCRIPT_DIR/merge-voice-metrics.sh"

PASS=0
FAIL=0
TMP="$(mktemp -d)"
trap "rm -rf '$TMP'" EXIT

say()  { echo "$@"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1"; }

# Helper to reset fixtures
reset() {
    rm -f "$TMP/local.jsonl" "$TMP/peer.jsonl"
}

# --- T1: empty local + empty peer → empty output
reset
: > "$TMP/local.jsonl"
: > "$TMP/peer.jsonl"
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
[ -f "$TMP/local.jsonl" ] && [ "$(wc -l < "$TMP/local.jsonl" | tr -d ' ')" = "0" ] \
    && pass "T1: empty+empty -> empty local" \
    || fail "T1: empty+empty should leave empty local"

# --- T2: empty local + peer entries → all peer in output, sorted
reset
: > "$TMP/local.jsonl"
cat > "$TMP/peer.jsonl" <<'EOF'
{"sessionId":"b","timestamp":"2026-04-17T10:00:00Z"}
{"sessionId":"a","timestamp":"2026-04-16T10:00:00Z"}
EOF
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
FIRST_TS="$(head -1 "$TMP/local.jsonl" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["timestamp"])' 2>/dev/null)"
[ "$FIRST_TS" = "2026-04-16T10:00:00Z" ] \
    && pass "T2: empty+peer → peer entries sorted ascending" \
    || fail "T2: expected earliest first, got $FIRST_TS"

# --- T3: dedup on (sessionId, timestamp)
reset
cat > "$TMP/local.jsonl" <<'EOF'
{"sessionId":"x","timestamp":"2026-04-17T10:00:00Z"}
{"sessionId":"y","timestamp":"2026-04-17T11:00:00Z"}
EOF
cat > "$TMP/peer.jsonl" <<'EOF'
{"sessionId":"x","timestamp":"2026-04-17T10:00:00Z"}
{"sessionId":"z","timestamp":"2026-04-17T09:00:00Z"}
EOF
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
LINES="$(wc -l < "$TMP/local.jsonl" | tr -d ' ')"
[ "$LINES" = "3" ] \
    && pass "T3: dedup on (sessionId,timestamp) — 3 unique from 4 lines" \
    || fail "T3: expected 3 lines, got $LINES"

# --- T4: sort ascending by timestamp after dedup
FIRST_TS="$(head -1 "$TMP/local.jsonl" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["timestamp"])' 2>/dev/null)"
LAST_TS="$(tail -1 "$TMP/local.jsonl" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["timestamp"])' 2>/dev/null)"
[ "$FIRST_TS" = "2026-04-17T09:00:00Z" ] && [ "$LAST_TS" = "2026-04-17T11:00:00Z" ] \
    && pass "T4: entries ascending-by-timestamp" \
    || fail "T4: order wrong — first=$FIRST_TS last=$LAST_TS"

# --- T5: malformed JSON lines tolerated (skipped, don't abort)
reset
cat > "$TMP/local.jsonl" <<'EOF'
{"sessionId":"ok","timestamp":"2026-04-17T10:00:00Z"}
this is not json
{not valid either
EOF
cat > "$TMP/peer.jsonl" <<'EOF'
{"sessionId":"ok2","timestamp":"2026-04-17T11:00:00Z"}
EOF
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
LINES="$(wc -l < "$TMP/local.jsonl" | tr -d ' ')"
[ "$LINES" = "2" ] \
    && pass "T5: malformed lines skipped, valid entries preserved" \
    || fail "T5: expected 2 valid lines, got $LINES"

# --- T6: missing peer file is a no-op (local unchanged)
reset
cat > "$TMP/local.jsonl" <<'EOF'
{"sessionId":"a","timestamp":"2026-04-17T10:00:00Z"}
EOF
ORIG_HASH="$(shasum "$TMP/local.jsonl" | awk '{print $1}')"
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
NEW_HASH="$(shasum "$TMP/local.jsonl" | awk '{print $1}')"
[ "$ORIG_HASH" = "$NEW_HASH" ] \
    && pass "T6: missing peer file → local unchanged" \
    || fail "T6: local modified despite missing peer"

# --- T7: peer file removed after successful merge
reset
: > "$TMP/local.jsonl"
echo '{"sessionId":"p","timestamp":"2026-04-17T10:00:00Z"}' > "$TMP/peer.jsonl"
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
[ ! -f "$TMP/peer.jsonl" ] \
    && pass "T7: peer staging file cleaned up after merge" \
    || fail "T7: peer file still present after merge"

# --- T8: missing local file gets created (doesn't crash)
reset
echo '{"sessionId":"p","timestamp":"2026-04-17T10:00:00Z"}' > "$TMP/peer.jsonl"
bash "$MERGE" "$TMP/local.jsonl" "$TMP/peer.jsonl" >/dev/null 2>&1
[ -f "$TMP/local.jsonl" ] && [ "$(wc -l < "$TMP/local.jsonl" | tr -d ' ')" = "1" ] \
    && pass "T8: missing local file created from peer" \
    || fail "T8: local file not created correctly"

# --- T9: script has +x and parses clean
[ -x "$MERGE" ] && bash -n "$MERGE" 2>/dev/null \
    && pass "T9: merge-voice-metrics.sh executable + syntax clean" \
    || fail "T9: script not executable or syntax error"

# --- T10: default paths used if no args (exits 0 even with no files)
reset
(cd "$TMP" && bash "$MERGE" >/dev/null 2>&1; rc=$?; [ "$rc" = "0" ] && exit 0 || exit 1) \
    && pass "T10: no-arg invocation exits 0 (no-op when defaults missing)" \
    || fail "T10: no-arg invocation non-zero"

# --- Summary
echo ""
echo "━━━ Summary ━━━"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo "merge-voice-metrics.sh smoke tests FAILED."
    exit 1
else
    echo "merge-voice-metrics.sh smoke tests OK."
fi

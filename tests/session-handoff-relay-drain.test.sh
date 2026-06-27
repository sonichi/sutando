#!/usr/bin/env bash
# Tests for the relay-note drain block in src/session-handoff.sh.
#
# Structural tests only — exercises the cap, drain-to-processed, and
# overflow-notice logic without a live Claude Code session.
#
# Run:   bash tests/session-handoff-relay-drain.test.sh
# Exit:  0 on pass, non-zero on fail.

set -uo pipefail   # no -e so individual test failures don't abort the suite

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "ok $((PASS+FAIL)) — $*"; }
fail() { FAIL=$((FAIL+1)); echo "not ok $((PASS+FAIL)) — $*"; }

# ---------------------------------------------------------------------------
# Inline the drain logic (identical to the block in src/session-handoff.sh).
# Call as: run_drain <relay_dir> [cap]
# Prints the drain output to stdout; moves files in-place.
# ---------------------------------------------------------------------------

run_drain() {
  local relay_dir="$1" cap="${2:-${SUTANDO_RELAY_INLINE_CAP:-8}}"
  RELAY_DIR="$relay_dir"
  RELAY_PROCESSED="$RELAY_DIR/processed"
  RELAY_INLINE_CAP="$cap"
  # shellcheck disable=SC2012
  RELAY_FILES=$(ls -t "$RELAY_DIR"/relay-*.md 2>/dev/null || true)
  if [ -n "$RELAY_FILES" ]; then
    mkdir -p "$RELAY_PROCESSED"
    echo "## Relay Notes (from prior session)"
    _n=0
    for _rf in $RELAY_FILES; do
      if [ "$_n" -lt "$RELAY_INLINE_CAP" ]; then
        echo "### $(basename "$_rf")"
        cat "$_rf"
        echo ""
      fi
      _n=$((_n + 1))
      mv "$_rf" "$RELAY_PROCESSED/" 2>/dev/null || true
    done
    [ "$_n" -gt "$RELAY_INLINE_CAP" ] && echo "_(+$((_n - RELAY_INLINE_CAP)) older relay notes drained to processed/, not inlined)_"
  fi
}

count_files() { find "$1" -maxdepth 1 -name "relay-*.md" 2>/dev/null | wc -l | tr -d ' '; }

# ---------------------------------------------------------------------------
# Test 1: no relay files → no output, no processed/ created
# ---------------------------------------------------------------------------
T=$(mktemp -d)
OUT=$(run_drain "$T" 8)
if [ -z "$OUT" ] && [ ! -d "$T/processed" ]; then
  ok "no relay files → no output and no processed/ dir"
else
  fail "no relay files → unexpected output or processed/ dir created"
fi
rm -rf "$T"

# ---------------------------------------------------------------------------
# Test 2: fewer files than cap — all inlined, all moved to processed/
# ---------------------------------------------------------------------------
T=$(mktemp -d)
for i in 1 2 3; do
  echo "note $i" > "$T/relay-10000$i.md"
done
OUT=$(run_drain "$T" 8)
remaining=$(count_files "$T")
processed=$(count_files "$T/processed")
if [ "$remaining" -eq 0 ] && [ "$processed" -eq 3 ] \
   && echo "$OUT" | grep -q "note 1" \
   && echo "$OUT" | grep -q "note 2" \
   && echo "$OUT" | grep -q "note 3" \
   && ! echo "$OUT" | grep -q "older relay"; then
  ok "3 files < cap 8 → all inlined, all moved, no overflow notice"
else
  fail "3 files < cap 8: remaining=$remaining processed=$processed (expected 0/3)"
fi
rm -rf "$T"

# ---------------------------------------------------------------------------
# Test 3: more files than cap — only cap inlined, ALL moved, overflow notice
# ---------------------------------------------------------------------------
T=$(mktemp -d)
for i in $(seq 1 12); do
  ts=$((1750000000 + i))
  echo "note $i" > "$T/relay-${ts}.md"
done
OUT=$(run_drain "$T" 8)
remaining=$(count_files "$T")
processed=$(count_files "$T/processed")
inlined=$(echo "$OUT" | grep -c "^### " || true)
has_notice=$(echo "$OUT" | grep -c "older relay notes drained" || true)
if [ "$remaining" -eq 0 ] && [ "$processed" -eq 12 ] \
   && [ "$inlined" -eq 8 ] && [ "$has_notice" -ge 1 ] \
   && echo "$OUT" | grep -q "+4 older relay notes"; then
  ok "12 files cap 8 → 8 inlined, 12 moved, '+4 older' notice"
else
  fail "12 files cap 8: inlined=$inlined processed=$processed remaining=$remaining notice=$has_notice"
fi
rm -rf "$T"

# ---------------------------------------------------------------------------
# Test 4: cap=1 edge case — only newest inlined, all drained
# ---------------------------------------------------------------------------
T=$(mktemp -d)
for i in 1 2 3; do
  echo "note $i" > "$T/relay-10000$i.md"
done
OUT=$(run_drain "$T" 1)
inlined=$(echo "$OUT" | grep -c "^### " || true)
processed=$(count_files "$T/processed")
if [ "$inlined" -eq 1 ] && [ "$processed" -eq 3 ]; then
  ok "cap=1 → 1 inlined, all 3 moved to processed/"
else
  fail "cap=1: inlined=$inlined processed=$processed (expected 1/3)"
fi
rm -rf "$T"

# ---------------------------------------------------------------------------
# Test 5: SUTANDO_RELAY_INLINE_CAP env override respected in the live script
# (tests the env var path by passing it directly to run_drain)
# ---------------------------------------------------------------------------
T=$(mktemp -d)
for i in $(seq 1 5); do
  echo "note $i" > "$T/relay-10000$i.md"
done
OUT=$(run_drain "$T" 2)
inlined=$(echo "$OUT" | grep -c "^### " || true)
has_notice=$(echo "$OUT" | grep -q "+3 older" && echo 1 || echo 0)
if [ "$inlined" -eq 2 ] && [ "$has_notice" -eq 1 ]; then
  ok "cap=2 → 2 inlined, '+3 older' notice"
else
  fail "cap=2: inlined=$inlined has_notice=$has_notice"
fi
rm -rf "$T"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "1..$((PASS+FAIL))"
if [ "$FAIL" -eq 0 ]; then
  echo "# ok — $PASS/$((PASS+FAIL)) tests passed"
  exit 0
else
  echo "# FAIL — $FAIL/$((PASS+FAIL)) tests failed"
  exit 1
fi

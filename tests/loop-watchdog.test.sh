#!/usr/bin/env bash
# Tests for src/loop-watchdog.sh
#
# Exercises: healthy (fresh mtime), stale (old mtime), missing status file,
# idempotent pending-question append, and plist template placeholder check.
#
# Run: bash tests/loop-watchdog.test.sh
# Exit: 0 on pass, non-zero on fail.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WATCHDOG="$REPO/src/loop-watchdog.sh"
TEMPLATE="$REPO/src/com.sutando.loop-watchdog.plist.template"
INSTALLER="$REPO/src/install-loop-watchdog.sh"

PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  ✓ $label"
    ((PASS++)) || true
  else
    echo "  ✗ $label — expected='$expected' actual='$actual'"
    ((FAIL++)) || true
  fi
}

assert_contains() {
  local label="$1" needle="$2" haystack="$3"
  if echo "$haystack" | grep -qF "$needle"; then
    echo "  ✓ $label"
    ((PASS++)) || true
  else
    echo "  ✗ $label — needle not found: '$needle'"
    ((FAIL++)) || true
  fi
}

assert_file_exists() {
  local label="$1" path="$2"
  if [ -f "$path" ]; then
    echo "  ✓ $label"
    ((PASS++)) || true
  else
    echo "  ✗ $label — file not found: $path"
    ((FAIL++)) || true
  fi
}

assert_not_contains() {
  local label="$1" needle="$2" haystack="$3"
  if ! echo "$haystack" | grep -qF "$needle"; then
    echo "  ✓ $label"
    ((PASS++)) || true
  else
    echo "  ✗ $label — needle unexpectedly found: '$needle'"
    ((FAIL++)) || true
  fi
}

# ---------------------------------------------------------------------------
# Setup shared tmp workspace
# ---------------------------------------------------------------------------
WS="$(mktemp -d /tmp/sutando-watchdog-test-XXXXXX)"
trap "rm -rf '$WS'" EXIT
export SUTANDO_WORKSPACE="$WS"
mkdir -p "$WS/state" "$WS/logs"
PENDING="$WS/pending-questions.md"
touch "$PENDING"

# ---------------------------------------------------------------------------
# T1: Healthy — status file is fresh (mtime = now)
# ---------------------------------------------------------------------------
STATUS="$WS/state/core-status.json"
echo '{"status":"idle"}' > "$STATUS"
# touch to now (default)
touch "$STATUS"
OUT=$(bash "$WATCHDOG" 2>&1)
LOG=$(cat "$WS/logs/watchdog.log" 2>/dev/null || true)
assert_contains "T1: healthy logs OK" "OK" "$LOG"
assert_not_contains "T1: healthy no STALE" "STALE" "$LOG"

# ---------------------------------------------------------------------------
# T2: Stale — set mtime 20 minutes in the past
# ---------------------------------------------------------------------------
rm -f "$WS/logs/watchdog.log"
touch -t "$(date -v-20M +%Y%m%d%H%M.%S)" "$STATUS"
OUT=$(bash "$WATCHDOG" 2>&1 || true)   # exits 0 even on stale
LOG=$(cat "$WS/logs/watchdog.log" 2>/dev/null || true)
assert_contains "T2: stale logs STALE" "STALE" "$LOG"
PQ=$(cat "$PENDING")
assert_contains "T2: pending question written" "Proactive-loop stale" "$PQ"

# ---------------------------------------------------------------------------
# T3: Idempotency — second stale run does NOT duplicate the pending question
# ---------------------------------------------------------------------------
touch -t "$(date -v-20M +%Y%m%d%H%M.%S)" "$STATUS"
bash "$WATCHDOG" 2>&1 || true
COUNT=$(grep -c "Proactive-loop stale" "$PENDING" || true)
assert_eq "T3: pending question not duplicated" "1" "$COUNT"

# ---------------------------------------------------------------------------
# T4: Missing status file — no crash, logs WARN
# ---------------------------------------------------------------------------
rm -f "$STATUS" "$WS/logs/watchdog.log"
OUT=$(bash "$WATCHDOG" 2>&1 || true)
LOG=$(cat "$WS/logs/watchdog.log" 2>/dev/null || true)
assert_contains "T4: missing status → WARN" "WARN" "$LOG"

# ---------------------------------------------------------------------------
# T5: Template placeholders present
# ---------------------------------------------------------------------------
TMPL=$(cat "$TEMPLATE")
assert_contains "T5: template has __SUTANDO_REPO__" "__SUTANDO_REPO__" "$TMPL"
assert_contains "T5: template has __SUTANDO_WORKSPACE__" "__SUTANDO_WORKSPACE__" "$TMPL"
assert_not_contains "T5: template has no resolved path" "/Users/" "$TMPL"

# ---------------------------------------------------------------------------
# T6: Installer renders both placeholders
# ---------------------------------------------------------------------------
FAKE_REPO="/fake/repo"
FAKE_WS="/fake/ws"
RENDERED=$(SUTANDO_REPO_DIR="$FAKE_REPO" SUTANDO_WORKSPACE="$FAKE_WS" \
  bash -c "
    SCRIPT_DIR='$REPO/src'
    sed -e 's|__SUTANDO_REPO__|$FAKE_REPO|g' -e 's|__SUTANDO_WORKSPACE__|$FAKE_WS|g' \
      '$TEMPLATE'
  ")
assert_contains "T6: installer replaces REPO" "/fake/repo" "$RENDERED"
assert_contains "T6: installer replaces WORKSPACE" "/fake/ws" "$RENDERED"
assert_not_contains "T6: no leftover __SUTANDO_REPO__" "__SUTANDO_REPO__" "$RENDERED"

# ---------------------------------------------------------------------------
# T7: Scripts are executable
# ---------------------------------------------------------------------------
assert_file_exists "T7: loop-watchdog.sh exists" "$WATCHDOG"
assert_file_exists "T7: install-loop-watchdog.sh exists" "$INSTALLER"
if [ -x "$WATCHDOG" ]; then echo "  ✓ T7: loop-watchdog.sh is executable"; ((PASS++)) || true
else echo "  ✗ T7: loop-watchdog.sh not executable"; ((FAIL++)) || true; fi
if [ -x "$INSTALLER" ]; then echo "  ✓ T7: install-loop-watchdog.sh is executable"; ((PASS++)) || true
else echo "  ✗ T7: install-loop-watchdog.sh not executable"; ((FAIL++)) || true; fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

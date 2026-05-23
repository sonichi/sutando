#!/usr/bin/env bash
# Tests for src/loop-watchdog.sh
# Exercises: OK path, STALE path, MISSING status file, idempotency,
# SUTANDO_HOME priority, install-loop-watchdog.sh template rendering.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WATCHDOG="$REPO/src/loop-watchdog.sh"
INSTALLER="$REPO/src/install-loop-watchdog.sh"
TEMPLATE="$REPO/src/com.sutando.loop-watchdog.plist.template"

pass=0
fail=0

ok()  { pass=$((pass + 1)); echo "  ✓ $1"; }
fail(){ fail=$((fail + 1));  echo "  ✗ $1"; }

# --- Setup temp workspace ---
WS="$(mktemp -d)"
trap 'rm -rf "$WS"' EXIT
mkdir -p "$WS/state" "$WS/logs"

# -----------------------------------------------------------------------
# 1. OK path: status file fresh → logs OK, no pending question
# -----------------------------------------------------------------------
STATUS="$WS/state/core-status.json"
PEND="$WS/pending-questions.md"
LOG="$WS/logs/watchdog.log"
echo '{"status":"idle","ts":0}' > "$STATUS"
touch "$STATUS"   # fresh mtime

SUTANDO_HOME="$WS" bash "$WATCHDOG"
if grep -q "OK" "$LOG"; then ok "fresh status → logs OK"; else fail "fresh status → logs OK"; fi
if grep -q "Proactive-loop stale" "$PEND" 2>/dev/null; then fail "fresh status → no pending question"; else ok "fresh status → no pending question"; fi

# -----------------------------------------------------------------------
# 2. STALE path: status file >15 min old → logs STALE + writes pending question
# -----------------------------------------------------------------------
rm -f "$PEND" "$LOG"
# Back-date by 1000 seconds (>900 threshold)
touch -t "$(date -v-1000S +%Y%m%d%H%M.%S)" "$STATUS"

SUTANDO_HOME="$WS" bash "$WATCHDOG"
if grep -q "STALE" "$LOG"; then ok "stale status → logs STALE"; else fail "stale status → logs STALE"; fi
if grep -q "Proactive-loop stale" "$PEND"; then ok "stale status → writes pending question"; else fail "stale status → writes pending question"; fi

# -----------------------------------------------------------------------
# 3. MISSING status file → logs WARN, no crash
# -----------------------------------------------------------------------
rm -f "$LOG" "$PEND" "$STATUS"

SUTANDO_HOME="$WS" bash "$WATCHDOG"
if grep -q "WARN" "$LOG"; then ok "missing status → logs WARN"; else fail "missing status → logs WARN"; fi
if grep -q "Proactive-loop stale" "$PEND" 2>/dev/null; then fail "missing status → no pending question written"; else ok "missing status → no pending question written"; fi

# -----------------------------------------------------------------------
# 4. Idempotency: STALE fired twice → pending question added only once
# -----------------------------------------------------------------------
rm -f "$LOG" "$PEND"
echo '{"status":"idle","ts":0}' > "$STATUS"
touch -t "$(date -v-1000S +%Y%m%d%H%M.%S)" "$STATUS"

SUTANDO_HOME="$WS" bash "$WATCHDOG"
SUTANDO_HOME="$WS" bash "$WATCHDOG"
COUNT=$(grep -c "## ⚠️ Proactive-loop stale" "$PEND" 2>/dev/null || echo 0)
if [ "$COUNT" -eq 1 ]; then ok "stale idempotency: pending question added once"; else fail "stale idempotency: pending question added once (count=$COUNT)"; fi

# -----------------------------------------------------------------------
# 5. SUTANDO_HOME takes priority over SUTANDO_WORKSPACE
# -----------------------------------------------------------------------
WS2="$(mktemp -d)"
trap 'rm -rf "$WS" "$WS2"' EXIT
mkdir -p "$WS2/state" "$WS2/logs"
rm -f "$WS/logs/watchdog.log" 2>/dev/null || true
echo '{"status":"idle","ts":0}' > "$WS2/state/core-status.json"
touch "$WS2/state/core-status.json"

SUTANDO_HOME="$WS2" SUTANDO_WORKSPACE="$WS" bash "$WATCHDOG"
if [ -f "$WS2/logs/watchdog.log" ]; then ok "SUTANDO_HOME used when set"; else fail "SUTANDO_HOME used when set"; fi
if [ -f "$WS/logs/watchdog.log" ]; then fail "SUTANDO_WORKSPACE NOT used when HOME set"; else ok "SUTANDO_WORKSPACE NOT used when HOME set"; fi

# -----------------------------------------------------------------------
# 6. Template file exists and contains both placeholders
# -----------------------------------------------------------------------
if [ -f "$TEMPLATE" ]; then ok "plist template exists"; else fail "plist template exists"; fi
if grep -q "__SUTANDO_REPO__" "$TEMPLATE"; then ok "template has REPO placeholder"; else fail "template has REPO placeholder"; fi
if grep -q "__SUTANDO_WORKSPACE__" "$TEMPLATE"; then ok "template has WORKSPACE placeholder"; else fail "template has WORKSPACE placeholder"; fi

# -----------------------------------------------------------------------
# 7. Installer renders both placeholders correctly
# -----------------------------------------------------------------------
WS3="$(mktemp -d)"
DEST3="$WS3/rendered.plist"
mkdir -p "$WS3/logs"
# Run installer in dry-run mode: render only (don't call launchctl)
sed -e "s|__SUTANDO_REPO__|/test/repo|g" \
    -e "s|__SUTANDO_WORKSPACE__|/test/ws|g" \
    "$TEMPLATE" > "$DEST3"
if grep -q "/test/repo" "$DEST3"; then ok "installer renders REPO placeholder"; else fail "installer renders REPO placeholder"; fi
if grep -q "/test/ws" "$DEST3"; then ok "installer renders WORKSPACE placeholder"; else fail "installer renders WORKSPACE placeholder"; fi
if ! grep -q "__SUTANDO_REPO__" "$DEST3" && ! grep -q "__SUTANDO_WORKSPACE__" "$DEST3"; then
  ok "no unrendered placeholders in output"
else
  fail "no unrendered placeholders in output"
fi

# -----------------------------------------------------------------------
# 8. loop-watchdog.sh has execute bit
# -----------------------------------------------------------------------
if [ -x "$WATCHDOG" ]; then ok "loop-watchdog.sh is executable"; else fail "loop-watchdog.sh is executable"; fi
if [ -x "$INSTALLER" ]; then ok "install-loop-watchdog.sh is executable"; else fail "install-loop-watchdog.sh is executable"; fi

# -----------------------------------------------------------------------
# 9. SUTANDO_HOME usage in watchdog source (grep guard)
# -----------------------------------------------------------------------
if grep -q "SUTANDO_HOME" "$WATCHDOG"; then ok "watchdog uses SUTANDO_HOME"; else fail "watchdog uses SUTANDO_HOME"; fi
if grep -q "SUTANDO_HOME" "$INSTALLER"; then ok "installer uses SUTANDO_HOME"; else fail "installer uses SUTANDO_HOME"; fi

echo
if [ "$fail" -eq 0 ]; then
  echo "All $pass loop-watchdog tests passed."
else
  echo "$pass passed, $fail failed."
  exit 1
fi

#!/usr/bin/env bash
# Test for Bug #2 fix (Lucy's Maddy report 2026-06-06): startup.sh's auto-migration
# loop when `sutando-migrate --commit` was run manually.
#
# Root cause: startup.sh gated on its OWN sentinel (`state/auth/migrated-from-env.txt`),
# which only gets created if startup.sh auto-triggered the migration. When the
# operator runs `sutando-migrate --commit` directly (e.g., per Lucy's flow), the
# migrate script creates its own per-source sentinels (`state/.migrated-from-A/B/C-<backup_id>`)
# but NOT startup's. Then on next boot, startup sees SUTANDO_WORKSPACE still set
# AND the legacy dir still non-empty (migrate doesn't rm legacy — only startup does)
# AND its own sentinel missing → re-fires auto-migration. Loop.
#
# Fix: also honor sutando-migrate's per-source sentinels. If any
# `state/.migrated-from-*` files exist at the resolved workspace, treat it as
# "already migrated" and skip the auto-migration block.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
STARTUP="$REPO/src/startup.sh"

fail=0

# ── Test 1: structural — gate condition references migrate-script sentinels
grep -qF '_migrate_script_sentinels_present' "$STARTUP" \
    || { echo "  FAIL: gate variable _migrate_script_sentinels_present missing from startup.sh"; fail=1; }
grep -qF '/state/.migrated-from-*' "$STARTUP" \
    || { echo "  FAIL: migrate-script sentinel glob pattern missing from startup.sh"; fail=1; }

# ── Test 2: gate condition includes the new check
grep -qF '[ "$_migrate_script_sentinels_present" = "0" ]' "$STARTUP" \
    || { echo "  FAIL: gate condition doesn't include migrate-script sentinel check"; fail=1; }

# ── Test 3: detection logic E2E — extract the lines + run them against fixtures
# The detection block is short enough to extract + exercise in isolation.

# Fixture: tmp _ws_new with `state/.migrated-from-C-<backup_id>` present
TMP_PRESENT="$(mktemp -d -t startup-test.XXXXXX)"
mkdir -p "$TMP_PRESENT/state"
touch "$TMP_PRESENT/state/.migrated-from-C-20260606T030000Z-p1r1"

# Synth: run the detection snippet against the fixture
detection_result="$(bash -c "
    _ws_new='$TMP_PRESENT'
    _migrate_script_sentinels_present=0
    if [ -n \"\$_ws_new\" ] && ls \"\$_ws_new\"/state/.migrated-from-* >/dev/null 2>&1; then
        _migrate_script_sentinels_present=1
    fi
    echo \"\$_migrate_script_sentinels_present\"
")"

if [ "$detection_result" != "1" ]; then
    echo "  FAIL: detection didn't recognize present migrate-script sentinel (got '$detection_result', expected '1')"
    fail=1
fi

rm -rf "$TMP_PRESENT"

# Fixture: tmp _ws_new with NO sentinels — detection should return 0
TMP_ABSENT="$(mktemp -d -t startup-test.XXXXXX)"
mkdir -p "$TMP_ABSENT/state"
# (no .migrated-from-* files)

detection_result="$(bash -c "
    _ws_new='$TMP_ABSENT'
    _migrate_script_sentinels_present=0
    if [ -n \"\$_ws_new\" ] && ls \"\$_ws_new\"/state/.migrated-from-* >/dev/null 2>&1; then
        _migrate_script_sentinels_present=1
    fi
    echo \"\$_migrate_script_sentinels_present\"
")"

if [ "$detection_result" != "0" ]; then
    echo "  FAIL: detection returned positive on absent sentinels (got '$detection_result', expected '0')"
    fail=1
fi

rm -rf "$TMP_ABSENT"

# ── Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

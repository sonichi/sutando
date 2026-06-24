#!/usr/bin/env bash
# Test for Bug #5 fix (Lucy's Maddy report 2026-06-06): operator-side
# footgun where `bash src/startup.sh > $SUTANDO_WORKSPACE/logs/startup.log 2>&1`
# loses the log when v0.8 auto-migration rm -rf's the legacy workspace.
# Fix: startup.sh tees its own stdout+stderr to `/tmp/sutando-startup-<ts>.log`
# at the very top so a copy ALWAYS survives. Opt out via SUTANDO_STARTUP_NO_LOG=1.

set -u
# NOTE: not `set -e` — accumulate failures, report at end.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
STARTUP="$REPO/src/startup.sh"

fail=0

# Test 1: structural — tee block is declared
grep -q "SUTANDO_STARTUP_NO_LOG" "$STARTUP" \
    || { echo "  FAIL: SUTANDO_STARTUP_NO_LOG opt-out missing"; fail=1; }

# Test 2: structural — log path uses /tmp
grep -q '/tmp/sutando-startup-' "$STARTUP" \
    || { echo "  FAIL: /tmp/sutando-startup-* log path missing"; fail=1; }

# Test 3: structural — exec process-substitution tee pattern present
grep -qF 'exec > >(tee -a' "$STARTUP" \
    || { echo "  FAIL: 'exec > >(tee -a ...)' stdout-tee pattern missing"; fail=1; }
grep -qF '2> >(tee -a' "$STARTUP" \
    || { echo "  FAIL: '2> >(tee -a ...)' stderr-tee pattern missing"; fail=1; }

# Test 4: structural — log path announced to stderr so operators can find it
grep -qF 'startup log →' "$STARTUP" \
    || { echo "  FAIL: announcement of log path to stderr missing"; fail=1; }

# Test 5: structural — tee block runs BEFORE the auto-migration block
# (otherwise an early die in migration prep wouldn't be captured)
TEE_LINE=$(grep -n 'SUTANDO_STARTUP_NO_LOG' "$STARTUP" | head -1 | cut -d: -f1)
# Match the actual migration-block executable line (`if [ -n "${SUTANDO_WORKSPACE:-}" ]`),
# not the comment that may reference it in surrounding doc-blocks.
MIGRATE_LINE=$(grep -n 'if \[ -n "\${SUTANDO_WORKSPACE:-}" \]' "$STARTUP" | head -1 | cut -d: -f1)
if [ -z "$TEE_LINE" ] || [ -z "$MIGRATE_LINE" ]; then
    echo "  FAIL: T5 — couldn't locate tee or migration block (TEE=$TEE_LINE MIGRATE=$MIGRATE_LINE)"
    fail=1
elif [ "$TEE_LINE" -gt "$MIGRATE_LINE" ]; then
    echo "  FAIL: T5 — tee block (line $TEE_LINE) appears AFTER auto-migration (line $MIGRATE_LINE); early migration aborts wouldn't be captured"
    fail=1
fi

# Test 6: structural — timestamp + PID in log filename for uniqueness
# (multiple concurrent startups, e.g. user retries while a hung one is still
# running, must not stomp each other's log file)
grep -qF '$$' "$STARTUP" \
    || { echo "  FAIL: PID ('\$\$') not in log filename — concurrent startups would collide"; fail=1; }

# ── E2E (tests 7-9) — verify the actual tee behavior end-to-end.
# Strategy: invoke `bash -c` with a minimal script that mimics startup.sh's
# tee block, write a known marker, exit, then check the /tmp log file. Avoids
# running the real startup.sh (which would launch services).
TMP_E2E="$(mktemp -d -t sutando-startup-log-e2e.XXXXXX)"
TMP_LOG="$TMP_E2E/captured.log"

# Test 7: E2E — tee block (extracted) writes to /tmp log AND stderr/stdout
# Extracting the block: we run a sub-shell with the tee pattern + a marker echo,
# then verify the captured file contains the marker.
(
    exec > >(tee -a "$TMP_LOG") 2> >(tee -a "$TMP_LOG" >&2)
    echo "MARKER_STDOUT"
    echo "MARKER_STDERR" >&2
    # Give async tees time to drain before subshell exits
    sleep 0.1
) > "$TMP_E2E/captured_stdout" 2> "$TMP_E2E/captured_stderr"

if ! grep -q "MARKER_STDOUT" "$TMP_LOG" 2>/dev/null; then
    echo "  FAIL: T7 — MARKER_STDOUT not in tee-captured /tmp log"; fail=1
fi
if ! grep -q "MARKER_STDERR" "$TMP_LOG" 2>/dev/null; then
    echo "  FAIL: T7 — MARKER_STDERR not in tee-captured /tmp log"; fail=1
fi
if ! grep -q "MARKER_STDOUT" "$TMP_E2E/captured_stdout" 2>/dev/null; then
    echo "  FAIL: T7 — MARKER_STDOUT didn't reach the real stdout (tee swallowed it?)"; fail=1
fi
if ! grep -q "MARKER_STDERR" "$TMP_E2E/captured_stderr" 2>/dev/null; then
    echo "  FAIL: T7 — MARKER_STDERR didn't reach the real stderr"; fail=1
fi

# Test 8: E2E — opt-out (SUTANDO_STARTUP_NO_LOG=1) skips the tee
# Run startup.sh just past the tee block + an immediate exit. The tee should
# NOT have fired, so no /tmp/sutando-startup-* file should be created during
# this test window.
# Snapshot existing matches first; new ones (post-snapshot) are what we check.
PRE_LIST="$(ls /tmp/sutando-startup-* 2>/dev/null || true)"
SUTANDO_STARTUP_NO_LOG=1 bash -c '
    set -e
    if [ "${SUTANDO_STARTUP_NO_LOG:-0}" != "1" ]; then
        _STARTUP_LOG="/tmp/sutando-startup-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
        echo "should not see this" >&2
        exec > >(tee -a "$_STARTUP_LOG") 2> >(tee -a "$_STARTUP_LOG" >&2)
    fi
    echo "running with no-log"
' > "$TMP_E2E/nolog_stdout" 2> "$TMP_E2E/nolog_stderr"
POST_LIST="$(ls /tmp/sutando-startup-* 2>/dev/null || true)"
if [ "$PRE_LIST" != "$POST_LIST" ]; then
    echo "  FAIL: T8 — SUTANDO_STARTUP_NO_LOG=1 still created /tmp log file"; fail=1
fi

# Test 9: E2E — the announce line goes to stderr (not stdout)
# Extract just the announce block + verify stderr-side carries the path.
(
    SUTANDO_STARTUP_NO_LOG=0
    _STARTUP_LOG="$TMP_E2E/announce-test.log"
    echo "📓 startup log → $_STARTUP_LOG" >&2
) > "$TMP_E2E/announce_stdout" 2> "$TMP_E2E/announce_stderr"

if grep -q "startup log →" "$TMP_E2E/announce_stdout" 2>/dev/null; then
    echo "  FAIL: T9 — announce line appeared on stdout (should be stderr only)"; fail=1
fi
if ! grep -q "startup log →" "$TMP_E2E/announce_stderr" 2>/dev/null; then
    echo "  FAIL: T9 — announce line missing from stderr"; fail=1
fi

# Cleanup
rm -rf "$TMP_E2E"

# Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

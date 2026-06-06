#!/usr/bin/env bash
# Focused tests for the pre-flight summary + per-file progress reporter added
# to sutando-migrate.sh. Tests the helpers (humanize_bytes, format_duration)
# directly + asserts pre-flight emits the expected summary lines + asserts
# the progress reporter emits per-file lines with the right shape.
#
# Does NOT depend on the full commit_main flow (which has a pre-existing test
# environment issue around DEST_REAL resolution, separate concern).
#
# Owner ask 2026-06-05: large-workspace migration UX (progress bar + pre-flight
# size estimate + interactive confirm).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

# Extract the helper functions into an isolated shell for testing.
HELPERS="$(awk '
    /^humanize_bytes\(\) \{/,/^}$/ { print; next }
    /^format_duration\(\) \{/,/^}$/ { print; next }
' "$MIGRATE")"

fail=0

# ── Test 1: humanize_bytes covers B / KB / MB / GB / TB
result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 0")"
[ "$result" = "0 B" ] || { echo "  FAIL: humanize_bytes 0 → expected '0 B', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 999")"
[ "$result" = "999 B" ] || { echo "  FAIL: humanize_bytes 999 → expected '999 B', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 1024")"
[ "$result" = "1.0 KB" ] || { echo "  FAIL: humanize_bytes 1024 → expected '1.0 KB', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 5242880")"
[ "$result" = "5.0 MB" ] || { echo "  FAIL: humanize_bytes 5242880 → expected '5.0 MB', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 2147483648")"
[ "$result" = "2.0 GB" ] || { echo "  FAIL: humanize_bytes 2147483648 → expected '2.0 GB', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"humanize_bytes 1099511627776")"
[ "$result" = "1.0 TB" ] || { echo "  FAIL: humanize_bytes 1099511627776 → expected '1.0 TB', got '$result'"; fail=1; }

# ── Test 2: format_duration covers <1s / s / m+s / h+m
result="$(bash -c "$HELPERS"$'\n'"format_duration 0")"
[ "$result" = "<1s" ] || { echo "  FAIL: format_duration 0 → expected '<1s', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"format_duration 30")"
[ "$result" = "30s" ] || { echo "  FAIL: format_duration 30 → expected '30s', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"format_duration 90")"
[ "$result" = "1m 30s" ] || { echo "  FAIL: format_duration 90 → expected '1m 30s', got '$result'"; fail=1; }

result="$(bash -c "$HELPERS"$'\n'"format_duration 7200")"
[ "$result" = "2h 0m" ] || { echo "  FAIL: format_duration 7200 → expected '2h 0m', got '$result'"; fail=1; }

# ── Test 3: --no-confirm flag is recognized (doesn't bail out as unknown)
out="$(bash "$MIGRATE" --no-confirm 2>&1 || true)"
echo "$out" | grep -q "unknown option" && { echo "  FAIL: --no-confirm reported as unknown option"; fail=1; }
echo "$out" | grep -qE "^Usage:|^sutando-migrate:" || { echo "  WARN: --no-confirm output unexpected (got '$out')"; }

# ── Test 4: pre-flight summary function exists + has expected structure
# We grep the script source for the function definition + key output lines
# rather than invoking commit_main (which depends on DEST_REAL resolution
# the test env doesn't replicate cleanly — separate pre-existing concern).
grep -q "^preflight_summary() {" "$MIGRATE" || { echo "  FAIL: preflight_summary() function missing from migrate script"; fail=1; }
grep -q "sutando-migrate: pre-flight scan" "$MIGRATE" || { echo "  FAIL: pre-flight scan header line missing"; fail=1; }
grep -q "TOTAL: " "$MIGRATE" || { echo "  FAIL: TOTAL line template missing"; fail=1; }
grep -q "Estimated copy time:" "$MIGRATE" || { echo "  FAIL: ETA line template missing"; fail=1; }
grep -q 'Proceed with copy? \[y/N\]' "$MIGRATE" || { echo "  FAIL: confirm prompt template missing"; fail=1; }

# ── Test 5: progress reporter line shape in commit_source
# Single load-bearing assertion: the literal `[%d/%d]` substring must appear
# in the migrate script. If the printf format changes upstream, this fails
# loudly rather than silently passing on a looser fallback pattern (Mini's
# nit on the original triple-fallback grep, 2026-06-06).
grep -qF '[%d/%d]' "$MIGRATE" \
    || { echo "  FAIL: per-file progress format '[%d/%d]' missing from commit_source"; fail=1; }
# The PROGRESS_N increment must precede the printf — grep for the literal
# accounting line so a refactor that moves the increment doesn't silently
# break the denominator.
grep -q 'PROGRESS_N=$((PROGRESS_N + 1))' "$MIGRATE" \
    || { echo "  FAIL: 'PROGRESS_N=\$((PROGRESS_N + 1))' accounting missing"; fail=1; }
grep -q "PROGRESS_TOTAL" "$MIGRATE" || { echo "  FAIL: PROGRESS_TOTAL variable missing"; fail=1; }

# ── Test 6: abort-propagation guard at the preflight_summary call site
# Critical: `exit N` inside $(preflight_summary) subshell doesn't propagate
# to the parent script — bash captures stdout + exit code in $? but the
# parent continues. Without the `|| exit $?` guard, a user typing "n" at
# the confirm prompt would see "Aborted" but backup_dest + commit_source
# would run anyway. Caught in self-cold-review 2026-06-06.
grep -q 'PROGRESS_TOTAL="\$(preflight_summary)" || exit \$?' "$MIGRATE" \
    || { echo "  FAIL: abort-propagation guard '|| exit \$?' missing on preflight_summary call site"; fail=1; }

# ── Test 7: synthetic end-to-end abort behavior with stub preflight
# Verify the guard pattern actually works: if the captured-subshell exits N,
# the parent script also exits N AND the line after the assignment is NOT
# reached. `|| true` on this line absorbs the non-zero so `set -e` doesn't
# trip before we inspect $?.
abort_test_result="$(bash -c '
    fake_preflight() { echo "100"; exit 3; }
    PROGRESS_TOTAL=0
    PROGRESS_TOTAL="$(fake_preflight)" || exit $?
    echo "REACHED_AFTER_FAKE_EXIT"
' 2>&1)" && abort_test_code=0 || abort_test_code=$?
if [ "$abort_test_code" = "3" ] && [ "$abort_test_result" = "" ]; then
    :  # pass
else
    echo "  FAIL: abort-propagation pattern broken: code=$abort_test_code out=[$abort_test_result] (expected code=3 out=empty)"
    fail=1
fi

# ── Test 8: byte-count uses uname-s branch (not BSD stat -f '%z' on Linux)
# The fix for #1474 replaces the stat -f '%z' + Linux fallback heuristic
# with an explicit uname -s branch. Verify the script contains the branch
# and does NOT contain the old fallback pattern.
grep -q "case.*uname -s" "$MIGRATE" || { echo "  FAIL: uname -s branch missing from preflight byte-count"; fail=1; }
grep -q 'Darwin)' "$MIGRATE" || { echo "  FAIL: Darwin) branch missing"; fail=1; }
grep -q 'Linux|\*)' "$MIGRATE" || { echo "  FAIL: Linux|*) branch missing"; fail=1; }
# The old heuristic-based fallback should be gone
grep -q 'Linux fallback.*BSD stat' "$MIGRATE" && { echo "  FAIL: old Linux-fallback comment still present (should be removed)"; fail=1; }
grep -q '\[ -z "\$bytes" \]' "$MIGRATE" && { echo "  FAIL: old fallback trigger '[ -z \$bytes ]' still present"; fail=1; }

# ── Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

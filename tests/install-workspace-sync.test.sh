#!/usr/bin/env bash
# Tests for scripts/install-workspace-sync.sh — the cross-platform installer
# that schedules the workspace-vault sync (macOS → LaunchAgent, Linux → cron).
#
# launchctl/crontab E2E would mutate the real system, so (like the sibling
# sync-workspace tests) we assert wiring structurally and mirror the pure
# interval→schedule logic.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALLER="$REPO/scripts/install-workspace-sync.sh"

fail=0

# ── Structural ──────────────────────────────────────────────────────────────

# Test 1: installer exists and is executable.
[ -x "$INSTALLER" ] || { echo "  FAIL: T1 — $INSTALLER missing or not executable"; fail=1; }

# Test 2: branches on OS (Darwin vs other).
grep -q 'uname -s' "$INSTALLER" \
    || { echo "  FAIL: T2 — does not detect OS via 'uname -s'"; fail=1; }
grep -q '"\$OS" = "Darwin"' "$INSTALLER" \
    || { echo "  FAIL: T2b — no Darwin branch"; fail=1; }

# Test 3: macOS path uses launchd (bootstrap), NOT crontab.
MACOS_FN="$(awk '/^macos_install\(\) \{/,/^}$/' "$INSTALLER")"
echo "$MACOS_FN" | grep -q 'launchctl bootstrap' \
    || { echo "  FAIL: T3 — macos_install does not 'launchctl bootstrap'"; fail=1; }
echo "$MACOS_FN" | grep -q 'crontab' \
    && { echo "  FAIL: T3b — macos_install touches crontab (must avoid the FDA-gated spool)"; fail=1; }

# Test 4: Linux path installs a crontab line.
grep -q '^cron_install()' "$INSTALLER" \
    || { echo "  FAIL: T4 — no cron_install for the Linux path"; fail=1; }
grep -q 'crontab -' "$INSTALLER" \
    || { echo "  FAIL: T4b — Linux path does not write via 'crontab -'"; fail=1; }

# Test 5: uninstall + status for both platforms.
for fn in macos_uninstall macos_status cron_uninstall cron_status; do
    grep -q "^$fn()" "$INSTALLER" || { echo "  FAIL: T5 — missing $fn"; fail=1; }
done

# Test 6: runs sync-workspace.sh in --default (bidirectional) mode.
grep -q '\-\-default' "$INSTALLER" \
    || { echo "  FAIL: T6 — job does not run sync-workspace.sh --default"; fail=1; }

# Test 7: sets GIT_SSH_COMMAND BatchMode (non-interactive SSH for headless sync).
grep -q 'GIT_SSH_COMMAND' "$INSTALLER" \
    || { echo "  FAIL: T7 — does not set GIT_SSH_COMMAND for non-interactive SSH"; fail=1; }

# Test 8: documents WHY launchd over cron (the FDA gotcha) so future readers
# don't "simplify" it back to crontab on macOS.
grep -qiE 'Full Disk Access|FDA|TCC' "$INSTALLER" \
    || { echo "  FAIL: T8 — missing the FDA/TCC rationale comment"; fail=1; }

# Test 9: syntax.
bash -n "$INSTALLER" || { echo "  FAIL: T9 — bash -n syntax error"; fail=1; }

# ── Logic: interval → cron schedule (mirror of cron_line) ───────────────────
_cron_sched() {  # arg: interval seconds → prints the schedule field
    local INTERVAL="$1" mins=$(( $1 / 60 ))
    [ "$mins" -ge 1 ] || mins=1
    if [ "$mins" -gt 59 ]; then echo "0 * * * *"; else echo "*/$mins * * * *"; fi
}
[ "$(_cron_sched 900)"  = "*/15 * * * *" ] || { echo "  FAIL: T10 — 900s should map to */15"; fail=1; }
[ "$(_cron_sched 1020)" = "*/17 * * * *" ] || { echo "  FAIL: T11 — 1020s should map to */17"; fail=1; }
[ "$(_cron_sched 3600)" = "0 * * * *" ]     || { echo "  FAIL: T12 — 3600s should cap to hourly"; fail=1; }

# ── Report ──────────────────────────────────────────────────────────────────
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

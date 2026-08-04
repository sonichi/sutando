#!/bin/bash
# SUTANDO_MIGRATE_DEST must redirect EVERY destination, not just DEST.
#
# The env var is declared in sutando-migrate.sh as a TEST hook for E2E fixtures.
# It overrides DEST — but the hook bridge and the channel bridge each resolve
# claude-sutando-config-dir independently via sutando-config.sh, so under a
# test-redirected migration they still wrote the OPERATOR's real config dir.
#
# Measured on a live host before the fix, alternating a 3s idle window against a
# 2s run of tests/sutando-migrate-argparse.test.py, three rounds:
#
#   round 1  IDLE(3s)       mtime same        round 1  TEST  MTIME CHANGED
#   round 2  IDLE(3s)       mtime same        round 2  TEST  MTIME CHANGED
#   round 3  IDLE(3s)       mtime same        round 3  TEST  MTIME CHANGED
#
# Content was IDENTICAL every time, because the hook install is idempotent. A
# content-hash comparison therefore reads "unchanged" and clears the test
# falsely; only mtime discriminates. That is why this file asserts on the skip
# message (deterministic, side-effect-free) AND keeps an mtime guard behind it.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

check() {  # check <name> <condition-exit> <detail>
    if [ "$2" -eq 0 ]; then echo "  ok   $1"; else
        echo "  FAIL $1 ${3:-}"; FAILURES=$((FAILURES + 1))
    fi
}

TMP="$(mktemp -d -t migrate-hook-bridge-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/dest" "$TMP/src"

CCD="$(bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir 2>/dev/null || true)"
SETTINGS="$CCD/settings.json"
BEFORE=""
[ -n "$CCD" ] && [ -f "$SETTINGS" ] && BEFORE="$(stat -f %m "$SETTINGS" 2>/dev/null || stat -c %Y "$SETTINGS" 2>/dev/null)"

echo "sutando-migrate: test hook covers the config dir, not just DEST"
echo "  (resolved config dir: ${CCD:-<none>})"

# --commit, NOT --dry-run: the bridges live in commit_main(), so a dry run
# never reaches them. My first version used --dry-run and produced IDENTICAL
# output with and without the fix — a reporter that cannot discriminate.
OUT="$(SUTANDO_MIGRATE_DEST="$TMP/dest" bash "$REPO/scripts/sutando-migrate.sh" --commit 2>&1)"

# 1. The guard announces itself. Silence would be indistinguishable from the
#    bridge having run and found nothing to do.
grep -q "hook bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$OUT"
check "hook bridge announces the test-redirect skip" $? "output did not contain the skip line"

grep -q "channel bridge: skipped (SUTANDO_MIGRATE_DEST set" <<<"$OUT"
check "channel bridge announces the test-redirect skip" $? "output did not contain the skip line"

# 2. The bridges must NOT report doing work.
! grep -q "bridging hooks via sutando-config-hooks.sh" <<<"$OUT"
check "hook bridge did not run" $? "the bridge announced an install under a redirected DEST"

! grep -q "bridging channels (Sutando bridge access lists" <<<"$OUT"
check "channel bridge did not run" $? "the bridge announced a copy under a redirected DEST"

# 3. Belt and braces: the operator's real settings.json must be untouched.
#    mtime, NOT content — the install is idempotent, so a hash comparison passes
#    even when the file was rewritten. Skipped (not passed) when absent, so an
#    unresolvable config dir cannot masquerade as a green assertion.
if [ -n "$BEFORE" ]; then
    AFTER="$(stat -f %m "$SETTINGS" 2>/dev/null || stat -c %Y "$SETTINGS" 2>/dev/null)"
    [ "$BEFORE" = "$AFTER" ]
    check "operator settings.json mtime unchanged" $? "mtime $BEFORE -> $AFTER (the bridge wrote the real config dir)"
else
    echo "  skip operator settings.json mtime (no settings.json at ${CCD:-<unresolved>})"
fi

echo
if [ "$FAILURES" -ne 0 ]; then echo "FAILED ($FAILURES)"; exit 1; fi
echo "All test-hook coverage checks passed."

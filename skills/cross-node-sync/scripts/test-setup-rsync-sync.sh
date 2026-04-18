#!/usr/bin/env bash
# test-setup-rsync-sync.sh — smoke tests for setup-rsync-sync.sh
#
# Runs the setup script in --dry-run / --setup / --help modes and asserts
# expected behaviors without performing any rsync or SSH transfer.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"
SCRIPT="skills/cross-node-sync/scripts/setup-rsync-sync.sh"

PASS=0
FAIL=0
pass() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL + 1)); }

echo "━━━ setup-rsync-sync.sh smoke tests ━━━"

# T1 — syntax parses
if bash -n "$SCRIPT" 2>/dev/null; then
    pass "T1: bash -n parses clean"
else
    fail "T1: bash -n syntax error"
fi

# T2 — --help exits 0 + prints header
HELP_OUT=$(bash "$SCRIPT" --help 2>&1)
if echo "$HELP_OUT" | grep -q "rsync-over-ssh cross-node sync"; then
    pass "T2: --help prints docstring"
else
    fail "T2: --help didn't print docstring"
fi

# T3 — --setup prints keypair guide without sync
SETUP_OUT=$(SUTANDO_SYNC_PEER=dummy bash "$SCRIPT" --setup 2>&1)
if echo "$SETUP_OUT" | grep -q "SSH key setup"; then
    pass "T3: --setup prints key setup guide"
else
    fail "T3: --setup didn't print key setup"
fi
if echo "$SETUP_OUT" | grep -q "authorized_keys"; then
    pass "T4: --setup mentions authorized_keys"
else
    fail "T4: --setup missing authorized_keys reference"
fi

# T5 — --dry-run requires SUTANDO_SYNC_PEER and exits 1 if unset
if PEER_UNSET=1 bash -c 'unset SUTANDO_SYNC_PEER; bash "$0" --dry-run' "$SCRIPT" >/dev/null 2>&1; then
    fail "T5: --dry-run without PEER env didn't error"
else
    rc=$?
    if [ "$rc" = "1" ]; then
        pass "T5: --dry-run with SUTANDO_SYNC_PEER unset exits 1"
    else
        fail "T5: wrong exit code $rc, expected 1"
    fi
fi

# T6 — --dry-run with PEER set prints DRY-RUN banner. Peer paths now default
# to local paths, so only SUTANDO_SYNC_PEER is needed.
DRY_OUT=$(SUTANDO_SYNC_PEER=dummy@localhost \
    bash "$SCRIPT" --dry-run 2>&1 || true)
if echo "$DRY_OUT" | grep -q "DRY-RUN MODE"; then
    pass "T6: --dry-run prints DRY-RUN banner"
else
    fail "T6: no DRY-RUN banner"
fi

# T7 — --dry-run output labels rsync invocations as [DRY]
if echo "$DRY_OUT" | grep -q "\[DRY\] would run: rsync"; then
    pass "T7: --dry-run labels rsync under [DRY] marker"
else
    fail "T7: rsync not labeled as [DRY]"
fi

# T8 — --dry-run output shows both memory and notes sync directions
MEM_LINES=$(echo "$DRY_OUT" | grep -c "Syncing memory/" || true)
NOTES_LINES=$(echo "$DRY_OUT" | grep -c "Syncing notes/" || true)
if [ "$MEM_LINES" -ge 1 ] && [ "$NOTES_LINES" -ge 1 ]; then
    pass "T8: --dry-run covers both memory/ and notes/ sync steps"
else
    fail "T8: missing memory/ ($MEM_LINES) or notes/ ($NOTES_LINES) sync step"
fi

# T9 — unknown arg exits 2
if bash "$SCRIPT" --bogus >/dev/null 2>&1; then
    fail "T9: unknown arg didn't error out"
else
    rc=$?
    if [ "$rc" = "2" ]; then
        pass "T9: unknown arg exits with code 2"
    else
        fail "T9: unknown arg exit code was $rc, expected 2"
    fi
fi

# T10 — expected exclusions present in rsync flags
if grep -q -- "--exclude '.DS_Store'" "$SCRIPT"; then
    pass "T10: .DS_Store exclusion present in rsync flags"
else
    fail "T10: .DS_Store exclusion missing"
fi

echo ""
echo "━━━ Summary ━━━"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
echo "setup-rsync-sync.sh smoke tests OK."

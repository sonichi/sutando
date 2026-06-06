#!/usr/bin/env bash
# Test for the auto-`--import` invocation in sutando-migrate.sh commit_main().
# Addresses Lucy's Maddy v0.8 migration report (2026-06-06 #design):
# sutando-migrate previously set up M2 directories but did NOT copy Claude
# memory from `~/.claude/projects/<slug>/*` to `<workspace>/.claude-sutando/
# projects/<slug>/*`. Owner's workaround was to run `bash scripts/
# sutando-shell-setup.sh --import` manually; now `commit_main` wires that
# automatically as the final step.
#
# This test verifies the wiring (flag parsing + call site shape) without
# requiring a full live --import run (which depends on rsync + actual
# ~/.claude/projects/ contents we don't want to mutate in a test).
# Structural checks only: the actual `--import` behavior is tested by
# sutando-shell-setup.sh's own tests.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

fail=0

# Test 1: --no-claude-import flag is recognized (doesn't bail as unknown)
out="$(bash "$MIGRATE" --no-claude-import 2>&1 || true)"
echo "$out" | grep -q "unknown option" && { echo "  FAIL: --no-claude-import reported as unknown option"; fail=1; }

# Test 2: NO_CLAUDE_IMPORT default + flag parser entry exist
grep -q "^NO_CLAUDE_IMPORT=0" "$MIGRATE" \
    || { echo "  FAIL: NO_CLAUDE_IMPORT default missing"; fail=1; }
grep -q -- "--no-claude-import" "$MIGRATE" \
    || { echo "  FAIL: --no-claude-import flag parser entry missing"; fail=1; }

# Test 3: commit_main invokes sutando-shell-setup.sh --import
grep -q "sutando-shell-setup.sh" "$MIGRATE" \
    || { echo "  FAIL: sutando-shell-setup.sh reference missing from migrate script"; fail=1; }
grep -qF 'bash "$_import_script" --import' "$MIGRATE" \
    || { echo "  FAIL: '--import' invocation pattern missing"; fail=1; }

# Test 4: invocation is gated on NO_CLAUDE_IMPORT + DELETE_SOURCE
grep -qF '[ "$NO_CLAUDE_IMPORT" = "0" ] && [ "$DELETE_SOURCE" = "0" ]' "$MIGRATE" \
    || { echo "  FAIL: NO_CLAUDE_IMPORT + DELETE_SOURCE gate missing"; fail=1; }

# Test 5: import failure is soft (doesn't hard-fail the migrate)
grep -q "FAILED.*re-run manually" "$MIGRATE" \
    || { echo "  FAIL: soft-fail recovery hint missing"; fail=1; }

# Test 6: structural assertion that the call site appears INSIDE commit_main()
# (not in some other code path). We verify the line is between the function
# header and the next top-level `}` boundary. Defense against a refactor that
# moves the import call out of commit_main.
COMMIT_MAIN_BLOCK="$(awk '/^commit_main\(\) \{/,/^}$/' "$MIGRATE")"
echo "$COMMIT_MAIN_BLOCK" | grep -q "sutando-shell-setup.sh" \
    || { echo "  FAIL: sutando-shell-setup.sh invocation not inside commit_main() function block"; fail=1; }
echo "$COMMIT_MAIN_BLOCK" | grep -qF '[ "$NO_CLAUDE_IMPORT" = "0" ]' \
    || { echo "  FAIL: NO_CLAUDE_IMPORT gate not inside commit_main()"; fail=1; }

# Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

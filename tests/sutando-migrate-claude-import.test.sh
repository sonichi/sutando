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

# ── Test 7: END-TO-END WIRING — auto-import announcement + clean migrate exit
# Per owner directive 2026-06-06 + feedback_e2e_tests_for_contributions: real
# E2E proves it works in the user's system, not just structural greps.
#
# **Honest scope note:** full destination isolation (asserting the FILES land
# in a tmp `<dest>/.claude-sutando/projects/<slug>/memory/`) requires either
# (a) refactoring `sutando-shell-setup.sh` to honor a pre-set CLAUDE_DIR env
# var, or (b) creating a fake-repo with its own sutando.config.json. Both are
# out of scope for this fix-PR. Empirically verified in a manual run during
# development that the auto-import DOES copy `~/.claude/projects/<slug>/*`
# files to the configured CLAUDE_DIR target (= the live workspace's
# `.claude-sutando/...`).
#
# This test verifies the END-TO-END WIRING in a tmp invocation:
# 1. Migrate runs to completion (exit 0)
# 2. The "invoking sutando-shell-setup.sh --import" announcement appears
# 3. The "Claude memory import: ok" success line appears (proves --import ran)
#
# These three together prove the wiring fires end-to-end. Asserting the
# specific destination path requires the isolation refactor above (filed as
# follow-up).
TMP_E2E="$(mktemp -d -t sutando-mig-e2e.XXXXXX)"
mkdir -p "$TMP_E2E/src-c/notes"
echo "src-c-content" > "$TMP_E2E/src-c/notes/foo.md"

# Run with bounded timeout via background-kill pattern (no `timeout` cmd on macOS)
e2e_out="$(
    ( SUTANDO_MIGRATE_DEST="$TMP_E2E/dest" \
      bash "$MIGRATE" commit --source C --no-confirm < /dev/null 2>&1 &
      PID=$!
      _slept=0
      while [ "$_slept" -lt 120 ]; do
          if ! kill -0 "$PID" 2>/dev/null; then break; fi
          sleep 1
          _slept=$((_slept+1))
      done
      kill -9 "$PID" 2>/dev/null || true
      wait "$PID" 2>/dev/null || true
    ) | tail -400
)"

# Assert: auto-import announcement line appears
if ! echo "$e2e_out" | grep -q "invoking sutando-shell-setup.sh"; then
    echo "  FAIL: E2E wiring — auto-import announcement line missing from output"
    echo "    Output tail:"
    echo "$e2e_out" | tail -10 | sed 's/^/      /'
    fail=1
fi

# Assert: import success ack appears (proves --import ran AND returned non-error)
if ! echo "$e2e_out" | grep -q "Claude memory import: ok"; then
    echo "  FAIL: E2E wiring — 'Claude memory import: ok' line missing — --import may have failed silently or not run"
    fail=1
fi

# Assert: migrate completed (the COMMIT complete banner appears)
if ! echo "$e2e_out" | grep -q "sutando-migrate: COMMIT complete"; then
    echo "  FAIL: E2E wiring — 'COMMIT complete' banner missing — migrate may have hung or errored"
    fail=1
fi

# Cleanup
rm -rf "$TMP_E2E"

# ── Test 8: slug-rename bridge code present (structural)
# Lucy's #design follow-up 2026-06-06: `--import` rsyncs same-slug, but if
# the Claude invocation CWD shifted between pre/post-M0 the slug differs and
# the same-slug rsync leaves files at the wrong slug. Bridge detects + copies.
grep -q "Claude memory bridge" "$MIGRATE" \
    || { echo "  FAIL: slug-rename bridge announcement missing from migrate"; fail=1; }
grep -qF 'cp -a "$_populated_dir/memory/"*.md' "$MIGRATE" \
    || { echo "  FAIL: bridge cp command pattern (cp -a) missing"; fail=1; }
grep -qF 'cp -an "$_populated_dir/memory/"*.md' "$MIGRATE" \
    && { echo "  FAIL: bridge still uses 'cp -an' — should be 'cp -a' (clobber stub by design per Lucy + Chi 2026-06-06)"; fail=1; }

# ── Test 9: E2E slug-rename bridge — pre-populate stub + populated, verify bridge
# Creates a tmp `.claude-sutando/projects/` layout with:
#   <base-slug>/memory/ — 3 .md files (the "populated")
#   <base-slug>-workspace/memory/MEMORY.md — 181-byte stub (Claude reads from here post-M0)
# Then runs the migrate (with --no-claude-import to skip the actual --import since
# we're testing the bridge in isolation) and asserts the 3 files appear at the
# stub's location after.
TMP_BRIDGE="$(mktemp -d -t sutando-mig-bridge.XXXXXX)"
mkdir -p "$TMP_BRIDGE/src-c/notes"
echo "src" > "$TMP_BRIDGE/src-c/notes/foo.md"
BASE_SLUG="-tmp-fake-slug"
mkdir -p "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}/memory"
mkdir -p "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}-workspace/memory"
echo "memory-1" > "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}/memory/test-1.md"
echo "memory-2" > "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}/memory/test-2.md"
echo "memory-3" > "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}/memory/test-3.md"
# REAL MEMORY.md at the populated source (simulates Lucy's ~73KB index)
echo "REAL-MEMORY-INDEX-content" > "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}/memory/MEMORY.md"
# Stub MEMORY.md at the variant — Claude wrote this on first read post-migration
echo "# stub" > "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}-workspace/memory/MEMORY.md"

bridge_out="$(
    ( SUTANDO_MIGRATE_DEST="$TMP_BRIDGE/dest" \
      bash "$MIGRATE" commit --source C --no-confirm --no-claude-import < /dev/null 2>&1 &
      PID=$!
      _slept=0
      while [ "$_slept" -lt 120 ]; do
          if ! kill -0 "$PID" 2>/dev/null; then break; fi
          sleep 1
          _slept=$((_slept+1))
      done
      kill -9 "$PID" 2>/dev/null || true
      wait "$PID" 2>/dev/null || true
    ) | tail -400
)"

if ! echo "$bridge_out" | grep -q "Claude memory bridge: ${BASE_SLUG} → ${BASE_SLUG}-workspace"; then
    echo "  FAIL: slug-rename bridge announcement missing for ${BASE_SLUG} → ${BASE_SLUG}-workspace"
    echo "    Output tail:"
    echo "$bridge_out" | tail -20 | sed 's/^/      /'
    fail=1
fi

for f in test-1.md test-2.md test-3.md; do
    if [ ! -f "$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}-workspace/memory/$f" ]; then
        echo "  FAIL: slug-rename bridge — $f not copied to workspace-suffix slug"
        fail=1
    fi
done

# Per Lucy + Chi 2026-06-06: the bridge MUST clobber the stub MEMORY.md
# with the populated source's real MEMORY.md. cp -an left the stub in
# place, hiding the real index from Claude.
variant_mem_md="$TMP_BRIDGE/dest/.claude-sutando/projects/${BASE_SLUG}-workspace/memory/MEMORY.md"
if [ ! -f "$variant_mem_md" ]; then
    echo "  FAIL: MEMORY.md missing at variant slug after bridge"
    fail=1
else
    actual_content="$(cat "$variant_mem_md")"
    if [ "$actual_content" = "# stub" ]; then
        echo "  FAIL: variant MEMORY.md still contains '# stub' — bridge cp -a did not clobber (regression to cp -an semantics)"
        fail=1
    elif [ "$actual_content" != "REAL-MEMORY-INDEX-content" ]; then
        echo "  FAIL: variant MEMORY.md content unexpected: '$actual_content'"
        fail=1
    fi
fi

rm -rf "$TMP_BRIDGE"

# Report
if [ "$fail" = "0" ]; then
    echo "ALL TESTS PASS"
else
    echo "TESTS FAILED"
    exit 1
fi

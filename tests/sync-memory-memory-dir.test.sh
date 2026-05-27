#!/bin/bash
# Source-inspection + behavioral tests for scripts/sync-memory.sh MEMORY_DIR
# resolution (PR #1079: honor explicit SUTANDO_MEMORY_DIR override).
#
# Prior to PR #1079, the resolution was always:
#   MEMORY_DIR="$HOME/.claude/projects/$(echo "$SCRIPT_PARENT" | sed 's|/|-|g')/memory"
#
# PR #1079 adds a three-branch resolution:
#   1. SUTANDO_MEMORY_DIR set   → use it (tilde-expanded)
#   2. SUTANDO_PRIVATE_DIR set  → use it + emit deprecation warning to stderr
#   3. Neither set              → fall back to SCRIPT_PARENT heuristic (unchanged)
#
# This file pins the structural and behavioral properties of that change so a
# future refactor can't silently regress the multi-checkout fix.
#
# Run:  bash tests/sync-memory-memory-dir.test.sh
# Exit: 0 on pass, 1 on fail.
#
# NOTE: source-inspection tests WILL FAIL on main until PR #1079 is merged —
# that is correct and expected. They are pre-emptive guards.

set -u
SCRIPT="${SYNC_MEMORY_SCRIPT:-$(cd "$(dirname "$0")/.." && pwd)/scripts/sync-memory.sh}"

if [ ! -f "$SCRIPT" ]; then
    echo "FAIL: $SCRIPT not found"
    exit 1
fi

PASS=0
FAIL=0

ok() {
    PASS=$((PASS + 1))
    echo "PASS: $1"
}

fail() {
    FAIL=$((FAIL + 1))
    echo "FAIL: $1${2:+ — $2}"
}

# ---------------------------------------------------------------------------
# Helper: extract the MEMORY_DIR resolution block from sync-memory.sh and
# run it in a subshell. Returns MEMORY_DIR=<value> on stdout; any stderr
# (e.g. deprecation warning) is emitted to the caller's stderr.
#
# The block is extracted by finding the line that assigns MEMORY_DIR (the
# if/elif/else block from PR #1079, or the bare assignment in older main)
# up to the NOTES_DIR= assignment that follows it. Running in a subshell
# with a fake SCRIPT_PARENT lets us verify the resolution in isolation.
# ---------------------------------------------------------------------------
_resolve_memory_dir() {
    local block
    block="$(awk '
        /^if \[ -n "\${SUTANDO_MEMORY_DIR/  { capture=1 }
        /^MEMORY_DIR="[^"]*SCRIPT_PARENT/   { if (!capture) { print; next } }
        capture && /^fi$/ { print; capture=0; exit }
        capture           { print }
        /^NOTES_DIR=/     { exit }
    ' "$SCRIPT")"

    if [ -z "$block" ]; then
        # Fallback: try to grab the bare MEMORY_DIR= single-line form (pre-#1079).
        block="$(grep '^MEMORY_DIR=.*SCRIPT_PARENT' "$SCRIPT" | head -1)"
    fi

    if [ -z "$block" ]; then
        echo "EXTRACT_FAILED"
        return 1
    fi

    local script_parent="${SCRIPT_PARENT_OVERRIDE:-/fake/repo/dir}"
    (
        SCRIPT_PARENT="$script_parent"
        eval "$block"
        echo "MEMORY_DIR=${MEMORY_DIR:-}"
    )
}

_get_memory_dir() {
    _resolve_memory_dir | grep '^MEMORY_DIR=' | sed 's/^MEMORY_DIR=//'
}

# Check whether the PR #1079 override block has been merged; skip behavioral
# tests that depend on it if not yet present.
_has_override_block() {
    grep -q 'SUTANDO_MEMORY_DIR' "$SCRIPT"
}

# ---------------------------------------------------------------------------
# Source-inspection tests — structural guards for PR #1079
# (these fail on main before the PR merges — expected behavior)
# ---------------------------------------------------------------------------

test_override_block_present() {
    if _has_override_block; then
        ok "SUTANDO_MEMORY_DIR override block is present in sync-memory.sh"
    else
        fail "SUTANDO_MEMORY_DIR override block is missing" \
             "sync-memory.sh must contain SUTANDO_MEMORY_DIR resolution (PR #1079)"
    fi
}

test_private_dir_deprecation_present() {
    if grep -q 'SUTANDO_PRIVATE_DIR' "$SCRIPT"; then
        ok "SUTANDO_PRIVATE_DIR deprecation path is present in sync-memory.sh"
    else
        fail "SUTANDO_PRIVATE_DIR deprecation path is missing" \
             "PR #1079 adds a SUTANDO_PRIVATE_DIR compat path with deprecation warning"
    fi
}

test_tilde_expansion_form_present() {
    if grep -qE '\$\{SUTANDO_MEMORY_DIR/#' "$SCRIPT"; then
        ok "tilde expansion uses safe \${VAR/#\~\$HOME} form (no eval)"
    else
        fail "safe tilde expansion form not found" \
             "expected \${SUTANDO_MEMORY_DIR/#\~\$HOME} in sync-memory.sh"
    fi
}

test_private_dir_emits_deprecation_warning() {
    if ! _has_override_block; then
        fail "SUTANDO_PRIVATE_DIR should emit deprecation warning" \
             "(override block not yet merged — PR #1079)"
        return
    fi
    local warning
    warning="$(unset SUTANDO_MEMORY_DIR 2>/dev/null; SUTANDO_PRIVATE_DIR="/tmp/legacy" _resolve_memory_dir 2>&1 | grep -v '^MEMORY_DIR=')"
    if echo "$warning" | grep -qi "deprecated"; then
        ok "SUTANDO_PRIVATE_DIR emits deprecation warning"
    else
        fail "SUTANDO_PRIVATE_DIR should emit a deprecation warning" \
             "got: '${warning:-(empty)}'"
    fi
}

# ---------------------------------------------------------------------------
# Behavioral tests — run the block and assert MEMORY_DIR value
# ---------------------------------------------------------------------------

test_explicit_override_absolute() {
    if ! _has_override_block; then
        fail "SUTANDO_MEMORY_DIR absolute override" "(override block not yet merged — PR #1079)"
        return
    fi
    local expected="/tmp/test-explicit-memory"
    local actual
    actual="$(SUTANDO_MEMORY_DIR="$expected" _get_memory_dir)"
    if [ "$actual" = "$expected" ]; then
        ok "SUTANDO_MEMORY_DIR absolute path is honored"
    else
        fail "SUTANDO_MEMORY_DIR absolute path not honored" \
             "expected '$expected', got '$actual'"
    fi
}

test_explicit_override_tilde() {
    if ! _has_override_block; then
        fail "SUTANDO_MEMORY_DIR tilde expansion" "(override block not yet merged — PR #1079)"
        return
    fi
    local actual
    actual="$(SUTANDO_MEMORY_DIR="~/memory-override" _get_memory_dir)"
    local expected="$HOME/memory-override"
    if [ "$actual" = "$expected" ]; then
        ok "SUTANDO_MEMORY_DIR tilde is expanded to \$HOME"
    else
        fail "SUTANDO_MEMORY_DIR tilde expansion failed" \
             "expected '$expected', got '$actual'"
    fi
}

test_unset_falls_back_to_heuristic() {
    local fake_script_parent="/Users/alice/repos/sutando"
    local expected_seg
    expected_seg="$(echo "$fake_script_parent" | sed 's|/|-|g')"
    local expected="$HOME/.claude/projects/$expected_seg/memory"
    local actual
    actual="$(unset SUTANDO_MEMORY_DIR 2>/dev/null; unset SUTANDO_PRIVATE_DIR 2>/dev/null; \
              SCRIPT_PARENT_OVERRIDE="$fake_script_parent" _get_memory_dir)"
    if [ "$actual" = "$expected" ]; then
        ok "unset SUTANDO_MEMORY_DIR falls back to SCRIPT_PARENT heuristic"
    else
        fail "fallback to SCRIPT_PARENT heuristic broken" \
             "expected '$expected', got '$actual'"
    fi
}

test_empty_string_falls_back_to_heuristic() {
    # Empty string is treated as unset by [ -n "${VAR:-}" ], so it must fall
    # through to the SCRIPT_PARENT heuristic just like an unset variable.
    if ! _has_override_block; then
        fail "empty SUTANDO_MEMORY_DIR should use heuristic" "(override block not yet merged — PR #1079)"
        return
    fi
    local fake_script_parent="/Users/alice/repos/sutando"
    local expected_seg
    expected_seg="$(echo "$fake_script_parent" | sed 's|/|-|g')"
    local expected="$HOME/.claude/projects/$expected_seg/memory"
    local actual
    actual="$(SUTANDO_MEMORY_DIR="" SUTANDO_PRIVATE_DIR="" SCRIPT_PARENT_OVERRIDE="$fake_script_parent" _get_memory_dir)"
    if [ "$actual" = "$expected" ]; then
        ok "empty SUTANDO_MEMORY_DIR falls back to SCRIPT_PARENT heuristic"
    else
        fail "empty SUTANDO_MEMORY_DIR should fall through to heuristic" \
             "expected '$expected', got '$actual'"
    fi
}

test_private_dir_resolves_memory_dir() {
    if ! _has_override_block; then
        fail "SUTANDO_PRIVATE_DIR fallback" "(override block not yet merged — PR #1079)"
        return
    fi
    local expected="/tmp/legacy-private-dir"
    local actual
    actual="$(unset SUTANDO_MEMORY_DIR 2>/dev/null; SUTANDO_PRIVATE_DIR="$expected" _get_memory_dir)"
    if [ "$actual" = "$expected" ]; then
        ok "SUTANDO_PRIVATE_DIR fallback resolves MEMORY_DIR correctly"
    else
        fail "SUTANDO_PRIVATE_DIR fallback did not set MEMORY_DIR" \
             "expected '$expected', got '$actual'"
    fi
}

test_memory_dir_beats_private_dir() {
    if ! _has_override_block; then
        fail "SUTANDO_MEMORY_DIR precedence over SUTANDO_PRIVATE_DIR" "(override block not yet merged — PR #1079)"
        return
    fi
    local expected="/tmp/memory-wins"
    local actual
    actual="$(SUTANDO_MEMORY_DIR="$expected" SUTANDO_PRIVATE_DIR="/tmp/should-lose" _get_memory_dir)"
    if [ "$actual" = "$expected" ]; then
        ok "SUTANDO_MEMORY_DIR takes precedence over SUTANDO_PRIVATE_DIR"
    else
        fail "SUTANDO_MEMORY_DIR should beat SUTANDO_PRIVATE_DIR" \
             "expected '$expected', got '$actual'"
    fi
}

test_multi_checkout_scenario() {
    # This is the bug this PR fixes: the agent's memory lives under checkout-A's
    # project key, but a cron invokes sync-memory from checkout-B. Without the
    # override, SCRIPT_PARENT points at B → wrong MEMORY_DIR. With the override,
    # the real memory dir wins.
    if ! _has_override_block; then
        fail "multi-checkout SUTANDO_MEMORY_DIR override" "(override block not yet merged — PR #1079)"
        return
    fi
    local real_memory="/Users/alice/.claude/projects/-Users-alice-checkout-a-sutando/memory"
    local fake_script_parent="/Users/alice/checkout-b/sutando"
    local actual
    actual="$(SUTANDO_MEMORY_DIR="$real_memory" SCRIPT_PARENT_OVERRIDE="$fake_script_parent" _get_memory_dir)"
    if [ "$actual" = "$real_memory" ]; then
        ok "multi-checkout: SUTANDO_MEMORY_DIR override wins over wrong SCRIPT_PARENT"
    else
        fail "multi-checkout: SUTANDO_MEMORY_DIR override did not win" \
             "expected '$real_memory', got '$actual'"
    fi
}

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

test_override_block_present
test_private_dir_deprecation_present
test_tilde_expansion_form_present
test_private_dir_emits_deprecation_warning
test_explicit_override_absolute
test_explicit_override_tilde
test_unset_falls_back_to_heuristic
test_empty_string_falls_back_to_heuristic
test_private_dir_resolves_memory_dir
test_memory_dir_beats_private_dir
test_multi_checkout_scenario

total=$((PASS + FAIL))
echo ""
echo "Results: $PASS/$total passed"
[ "$FAIL" -eq 0 ]

#!/bin/bash
# Tests for scripts/sync-memory.sh SUTANDO_MEMORY_DIR override (PR #1079).
#
# Run: bash tests/sync-memory-dir-override.test.sh
# Exit: 0 on pass, 1 on fail.
#
# Approach: extract the MEMORY_DIR assignment block from sync-memory.sh and
# evaluate it in an isolated subshell with controlled env, asserting the
# resulting MEMORY_DIR value under three scenarios.

set -u
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/scripts/sync-memory.sh"

if [ ! -f "$SCRIPT" ]; then
    echo "FAIL: $SCRIPT not found"
    exit 1
fi

# Extract the MEMORY_DIR block (from the "Explicit override" comment to the
# closing `fi` that ends the if/else).
MEMORY_DIR_BLOCK="$(awk '
    /Explicit override \(SUTANDO_MEMORY_DIR\)/  { capture = 1 }
    capture                                      { print }
    capture && /^fi$/                            { capture = 0; exit }
' "$SCRIPT")"

if [ -z "$MEMORY_DIR_BLOCK" ]; then
    echo "FAIL: could not extract MEMORY_DIR block from $SCRIPT"
    echo "      (anchor comment changed — update the awk pattern)"
    exit 1
fi

PASS=0
FAIL=0

pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------------------
# Test 1: SUTANDO_MEMORY_DIR set → MEMORY_DIR follows it exactly.
# ---------------------------------------------------------------------------
RESULT=$(
    SUTANDO_MEMORY_DIR="/custom/memory/path"
    SCRIPT_PARENT="/some/checkout/sutando"
    HOME=/fakehome
    eval "$MEMORY_DIR_BLOCK"
    echo "$MEMORY_DIR"
)
if [ "$RESULT" = "/custom/memory/path" ]; then
    pass "explicit SUTANDO_MEMORY_DIR is honored"
else
    fail "explicit SUTANDO_MEMORY_DIR: expected /custom/memory/path, got $RESULT"
fi

# ---------------------------------------------------------------------------
# Test 2: SUTANDO_MEMORY_DIR unset → falls back to SCRIPT_PARENT heuristic.
# ---------------------------------------------------------------------------
RESULT=$(
    unset SUTANDO_MEMORY_DIR 2>/dev/null || true
    SCRIPT_PARENT="/Users/alice/Projects/sutando"
    HOME=/Users/alice
    eval "$MEMORY_DIR_BLOCK"
    echo "$MEMORY_DIR"
)
EXPECTED="/Users/alice/.claude/projects/-Users-alice-Projects-sutando/memory"
if [ "$RESULT" = "$EXPECTED" ]; then
    pass "unset SUTANDO_MEMORY_DIR falls back to SCRIPT_PARENT heuristic"
else
    fail "fallback heuristic: expected $EXPECTED, got $RESULT"
fi

# ---------------------------------------------------------------------------
# Test 3: Tilde (~) in SUTANDO_MEMORY_DIR is expanded to HOME.
# ---------------------------------------------------------------------------
RESULT=$(
    SUTANDO_MEMORY_DIR="~/my-memory"
    SCRIPT_PARENT="/some/checkout"
    HOME=/Users/bob
    eval "$MEMORY_DIR_BLOCK"
    echo "$MEMORY_DIR"
)
if [ "$RESULT" = "/Users/bob/my-memory" ]; then
    pass "tilde in SUTANDO_MEMORY_DIR expands to HOME"
else
    fail "tilde expansion: expected /Users/bob/my-memory, got $RESULT"
fi

# ---------------------------------------------------------------------------
# Test 4: SUTANDO_MEMORY_DIR empty string ("") → uses heuristic (not empty).
# ---------------------------------------------------------------------------
RESULT=$(
    SUTANDO_MEMORY_DIR=""
    SCRIPT_PARENT="/Users/carol/sutando"
    HOME=/Users/carol
    eval "$MEMORY_DIR_BLOCK"
    echo "$MEMORY_DIR"
)
if [ "$RESULT" = "/Users/carol/.claude/projects/-Users-carol-sutando/memory" ]; then
    pass "empty SUTANDO_MEMORY_DIR falls back to heuristic"
else
    fail "empty SUTANDO_MEMORY_DIR: expected heuristic path, got $RESULT"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1

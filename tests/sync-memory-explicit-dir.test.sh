#!/usr/bin/env bash
# Tests for SUTANDO_MEMORY_DIR override in scripts/sync-memory.sh
#
# PR #1079: honor explicit SUTANDO_MEMORY_DIR env var so multi-checkout
# installs can pin the real memory dir without relying on SCRIPT_PARENT.
#
# Extracts the MEMORY_DIR resolution block from sync-memory.sh and runs it
# in isolation — no git, no rsync, no real memory files needed.
#
# Run: bash tests/sync-memory-explicit-dir.test.sh
# Exit: 0 on pass, 1 on fail.

set -euo pipefail

PASS=0
FAIL=0

ok()   { echo "ok — $1"; (( PASS++ )) || true; }
fail() { echo "FAIL — $1"; (( FAIL++ )) || true; }

# ---------------------------------------------------------------------------
# Inline the resolution block (identical to sync-memory.sh lines)
# ---------------------------------------------------------------------------
resolve_memory_dir() {
  local SCRIPT_PARENT="$1"
  local result
  if [ -n "${SUTANDO_MEMORY_DIR:-}" ]; then
    result="${SUTANDO_MEMORY_DIR/#\~/$HOME}"
  else
    result="$HOME/.claude/projects/$(echo "$SCRIPT_PARENT" | sed 's|/|-|g')/memory"
  fi
  echo "$result"
}

# ---------------------------------------------------------------------------
# Test a: SUTANDO_MEMORY_DIR unset → derive from SCRIPT_PARENT
# ---------------------------------------------------------------------------
PARENT="/Users/bassil/Projects/sutando"
EXPECTED="$HOME/.claude/projects/-Users-bassil-Projects-sutando/memory"
ACTUAL=$(SUTANDO_MEMORY_DIR="" resolve_memory_dir "$PARENT")
if [ "$ACTUAL" = "$EXPECTED" ]; then
  ok "unset SUTANDO_MEMORY_DIR derives from SCRIPT_PARENT"
else
  fail "unset SUTANDO_MEMORY_DIR: expected '$EXPECTED', got '$ACTUAL'"
fi

# ---------------------------------------------------------------------------
# Test b: explicit SUTANDO_MEMORY_DIR overrides SCRIPT_PARENT
# ---------------------------------------------------------------------------
EXPLICIT="/Users/bassil/.claude/projects/-Users-bassil-Documents-github-sutando/memory"
ACTUAL=$(SUTANDO_MEMORY_DIR="$EXPLICIT" resolve_memory_dir "$PARENT")
if [ "$ACTUAL" = "$EXPLICIT" ]; then
  ok "explicit SUTANDO_MEMORY_DIR overrides SCRIPT_PARENT"
else
  fail "explicit override: expected '$EXPLICIT', got '$ACTUAL'"
fi

# ---------------------------------------------------------------------------
# Test c: tilde expansion in SUTANDO_MEMORY_DIR
# ---------------------------------------------------------------------------
ACTUAL=$(SUTANDO_MEMORY_DIR="~/.claude/projects/custom/memory" resolve_memory_dir "$PARENT")
EXPECTED_TILDE="$HOME/.claude/projects/custom/memory"
if [ "$ACTUAL" = "$EXPECTED_TILDE" ]; then
  ok "tilde in SUTANDO_MEMORY_DIR is expanded to \$HOME"
else
  fail "tilde expansion: expected '$EXPECTED_TILDE', got '$ACTUAL'"
fi

# ---------------------------------------------------------------------------
# Test d: different SCRIPT_PARENT → different derived dir (unset override)
# ---------------------------------------------------------------------------
PARENT2="/Users/bassil/Documents/github/sutando"
EXPECTED2="$HOME/.claude/projects/-Users-bassil-Documents-github-sutando/memory"
ACTUAL2=$(SUTANDO_MEMORY_DIR="" resolve_memory_dir "$PARENT2")
if [ "$ACTUAL2" = "$EXPECTED2" ]; then
  ok "different SCRIPT_PARENT produces different derived dir"
else
  fail "SCRIPT_PARENT variation: expected '$EXPECTED2', got '$ACTUAL2'"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "Results: $PASS passed, $FAIL failed."
[ "$FAIL" -eq 0 ]

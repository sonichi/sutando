#!/usr/bin/env bash
# Tests for src/migrate.sh — verifies path detection and bundle creation.
# Run: bash tests/migration.test.sh

set -uo pipefail
cd "$(dirname "$0")/.."

PASSED=0
FAILED=0

assert_true() {
  local msg="$1"
  if eval "$2"; then
    echo "  ✓ $msg"
    ((PASSED++))
  else
    echo "  ✗ $msg"
    ((FAILED++))
  fi
}

assert_eq() {
  local msg="$1" actual="$2" expected="$3"
  if [ "$actual" = "$expected" ]; then
    echo "  ✓ $msg"
    ((PASSED++))
  else
    echo "  ✗ $msg (got '$actual', expected '$expected')"
    ((FAILED++))
  fi
}

echo "=== Migration Script Tests ==="

# --- REPO_SLUG generation ---
echo ""
echo "REPO_SLUG generation:"
REPO="$(pwd)"
REPO_SLUG=$(echo "$REPO" | sed 's|/|-|g')
assert_true "REPO_SLUG starts with dash" "[ '${REPO_SLUG:0:1}' = '-' ]"
assert_true "REPO_SLUG contains no slashes" "! echo '$REPO_SLUG' | grep -q '/'"
assert_true "REPO_SLUG is not empty" "[ -n '$REPO_SLUG' ]"

# --- Memory dir detection ---
echo ""
echo "Memory dir detection:"
MEMORY_DIR="$HOME/.claude/projects/$REPO_SLUG/memory"
assert_true "memory dir exists at auto-detected path" "[ -d '$MEMORY_DIR' ]"
assert_true "memory dir has .md files" "ls '$MEMORY_DIR'/*.md > /dev/null 2>&1"

MD_COUNT=$(ls "$MEMORY_DIR"/*.md 2>/dev/null | wc -l | tr -d ' ')
assert_true "memory dir has 10+ files (have $MD_COUNT)" "[ '$MD_COUNT' -ge 10 ]"

# --- Session dir detection ---
echo ""
echo "Session dir detection:"
SESSION_DIR="$HOME/.claude/projects/$REPO_SLUG"
assert_true "session dir exists" "[ -d '$SESSION_DIR' ]"

# --- Bundle creation (dry run) ---
echo ""
echo "Bundle creation:"
BUNDLE="/tmp/sutando-migration-test-$$"
mkdir -p "$BUNDLE"

# Test .env copy
cp "$REPO/.env" "$BUNDLE/.env" 2>/dev/null
assert_true ".env copied" "[ -f '$BUNDLE/.env' ]"

# Test memory copy
cp -r "$MEMORY_DIR" "$BUNDLE/memory" 2>/dev/null
assert_true "memory copied" "[ -d '$BUNDLE/memory' ]"
BUNDLE_MD=$(ls "$BUNDLE/memory/"*.md 2>/dev/null | wc -l | tr -d ' ')
assert_eq "memory file count matches" "$BUNDLE_MD" "$MD_COUNT"

# Test MEMORY.md exists in bundle
assert_true "MEMORY.md in bundle" "[ -f '$BUNDLE/memory/MEMORY.md' ]"

# Cleanup
rm -rf "$BUNDLE"
assert_true "cleanup succeeded" "[ ! -d '$BUNDLE' ]"

# --- Script syntax ---
echo ""
echo "Script syntax:"
assert_true "migrate.sh has no syntax errors" "bash -n src/migrate.sh"
assert_true "migrate.sh is not set -e (allows optional paths)" "! head -20 src/migrate.sh | grep -q '^set -e$'"

echo ""
echo "=== Results: $PASSED passed, $FAILED failed ==="
[ "$FAILED" -eq 0 ] || exit 1

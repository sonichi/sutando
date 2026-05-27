#!/usr/bin/env bash
# Tests for scripts/check-user-hardcoded.sh.
#
# 1. Runs the lint against the real repo — must pass (exit 0).
# 2. Creates a synthetic dir with a hardcoded literal — lint must catch (exit 1).
#
# Run: bash tests/check-user-hardcoded.test.sh
# Exit: 0 on pass, 1 on fail.

set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/scripts/check-user-hardcoded.sh"
PASS=0
FAIL=0

assert_exit() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  ✓ $desc"
    PASS=$((PASS + 1))
  else
    echo "  ✗ $desc — expected exit $expected, got $actual"
    FAIL=$((FAIL + 1))
  fi
}

echo "Test 1: lint passes on the real repo"
out=$(bash "$SCRIPT" 2>&1)
assert_exit "real repo is clean" 0 $?

echo
echo "Test 2: lint catches a synthetic violation"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/fake-skill/scripts"
cat > "$TMP/fake-skill/scripts/violator.py" <<'EOF'
# Synthetic violation — should be caught.
GWS_BIN = "/Users/qingyunwu/.local/bin/gws"
OWNER_PHONE = "+14344664925"
EOF
cd "$TMP"
CHECK_USER_HARDCODED_ROOTS=":fake-skill" bash "$SCRIPT" > /tmp/check-lint.out 2>&1
assert_exit "lint catches violation in synthetic file" 1 $?
if grep -q '/Users/qingyunwu' /tmp/check-lint.out && grep -q '+14344664925' /tmp/check-lint.out; then
  echo "  ✓ output names both literals"
  PASS=$((PASS + 1))
else
  echo "  ✗ output missing expected literal mentions:"
  cat /tmp/check-lint.out
  FAIL=$((FAIL + 1))
fi
cd "$REPO"

echo
echo "Test 3: lint excludes itself from results"
out=$(bash "$SCRIPT" 2>&1)
if echo "$out" | grep -q 'check-user-hardcoded.sh:'; then
  echo "  ✗ self-references not filtered"
  FAIL=$((FAIL + 1))
else
  echo "  ✓ self filtered out"
  PASS=$((PASS + 1))
fi

echo
echo "Ran $((PASS + FAIL)) tests. Passed: $PASS. Failed: $FAIL."
[ $FAIL -eq 0 ]

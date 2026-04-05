#!/usr/bin/env bash
# Run all Sutando tests. Exit 1 if any fail.
set -uo pipefail
cd "$(dirname "$0")/.."

FAILED=0
PASSED=0

run_test() {
  local name="$1" cmd="$2"
  printf "%-40s " "$name"
  if eval "$cmd" > /dev/null 2>&1; then
    echo "✓"
    ((PASSED++))
  else
    echo "✗"
    ((FAILED++))
  fi
}

echo "=== Sutando Test Suite ==="
echo

run_test "Phone access control (TS)" "npx tsx tests/phone-access-control.test.ts"
run_test "Browser tools / STT (TS)" "npx tsx tests/browser-tools.test.ts"
run_test "Security sanitization (Python)" "python3 tests/security-sanitization.test.py"
run_test "Call log scanner (Python)" "python3 tests/call-log-scanner.test.py"

echo
echo "Passed: $PASSED  Failed: $FAILED"
[ "$FAILED" -eq 0 ] || exit 1

#!/usr/bin/env bash
# Tests for the src/startup.sh wire-up of scripts/sutando-shell-setup.sh
#
# The wire-up is intentionally minimal (3 lines, src/startup.sh:210-212):
#
#   if [ -x "$REPO/scripts/sutando-shell-setup.sh" ]; then
#     bash "$REPO/scripts/sutando-shell-setup.sh" --auto || true
#   fi
#
# These tests cover three branches:
#   1. helper script missing                  → block is a no-op, startup continues
#   2. helper script exits non-zero (decline) → `|| true` swallows, startup continues
#   3. helper script exits 0  (success / already configured) → startup continues
#
# Strategy: extract the wire-up block into a tiny driver that ALSO writes a
# sentinel "still-running" file after the block. Each test stubs the helper
# script to the desired exit code, runs the driver, and asserts the sentinel
# was written — proving startup.sh continues past the wire-up.
#
# Run:    bash tests/startup-shell-setup-wireup.test.sh
# Exit:   0 on pass, 1 on first failure.

set -uo pipefail

PASS=0
FAIL=0

run_test() {
  local name="$1"; shift
  printf '%-60s' "$name"
  if "$@"; then
    echo "ok"
    PASS=$((PASS + 1))
  else
    echo "FAIL"
    FAIL=$((FAIL + 1))
  fi
}

# Build a temp fake repo + driver in $TEST_TMP. The driver mirrors startup.sh's
# wire-up block exactly, then writes "$TEST_TMP/sentinel" so we can prove
# control flow continued.
make_driver_with_stub_helper() {
  local exit_code="$1"      # exit code the stub helper should return ('absent' to skip creating it)
  TEST_TMP="$(mktemp -d -t startup-shell-setup-wireup.XXXXXX)"
  mkdir -p "$TEST_TMP/scripts"

  if [ "$exit_code" != "absent" ]; then
    cat > "$TEST_TMP/scripts/sutando-shell-setup.sh" << EOF
#!/usr/bin/env bash
exit $exit_code
EOF
    chmod +x "$TEST_TMP/scripts/sutando-shell-setup.sh"
  fi

  cat > "$TEST_TMP/driver.sh" << 'EOF'
#!/usr/bin/env bash
# Mirror of src/startup.sh:210-212 wire-up block.
REPO="$1"
SENTINEL="$2"

# This is the exact block from startup.sh — keep these 3 lines in sync.
if [ -x "$REPO/scripts/sutando-shell-setup.sh" ]; then
  bash "$REPO/scripts/sutando-shell-setup.sh" --auto || true
fi

# If startup.sh continues past the wire-up, we'd reach here.
touch "$SENTINEL"
EOF
  chmod +x "$TEST_TMP/driver.sh"
}

cleanup_driver() {
  [ -n "${TEST_TMP:-}" ] && rm -rf "$TEST_TMP" || true
  unset TEST_TMP
}

# ----------------------------------------------------------------------
# 1. helper script ABSENT → wire-up block is a no-op, driver continues
# ----------------------------------------------------------------------
test_helper_absent() {
  make_driver_with_stub_helper "absent"
  bash "$TEST_TMP/driver.sh" "$TEST_TMP" "$TEST_TMP/sentinel" 2>/dev/null
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: driver exited non-zero ($rc) when helper absent"
    cleanup_driver
    return 1
  fi
  if [ ! -f "$TEST_TMP/sentinel" ]; then
    echo "  FAIL: sentinel not written — startup didn't continue past wire-up"
    cleanup_driver
    return 1
  fi
  cleanup_driver
  return 0
}

# ----------------------------------------------------------------------
# 2. helper EXITS NON-ZERO (e.g. user declined --auto prompt) → || true
#    swallows the failure, driver continues
# ----------------------------------------------------------------------
test_helper_exits_nonzero() {
  make_driver_with_stub_helper "2"  # 2 = user declined per the helper's contract
  bash "$TEST_TMP/driver.sh" "$TEST_TMP" "$TEST_TMP/sentinel" 2>/dev/null
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: driver exited non-zero ($rc) — the '|| true' didn't swallow"
    cleanup_driver
    return 1
  fi
  if [ ! -f "$TEST_TMP/sentinel" ]; then
    echo "  FAIL: sentinel not written — startup aborted past the wire-up"
    cleanup_driver
    return 1
  fi
  cleanup_driver
  return 0
}

# ----------------------------------------------------------------------
# 3. helper EXITS ZERO → driver continues normally
# ----------------------------------------------------------------------
test_helper_exits_zero() {
  make_driver_with_stub_helper "0"
  bash "$TEST_TMP/driver.sh" "$TEST_TMP" "$TEST_TMP/sentinel" 2>/dev/null
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: driver exited non-zero ($rc) on helper success"
    cleanup_driver
    return 1
  fi
  if [ ! -f "$TEST_TMP/sentinel" ]; then
    echo "  FAIL: sentinel not written"
    cleanup_driver
    return 1
  fi
  cleanup_driver
  return 0
}

# ----------------------------------------------------------------------
# 4. Verify the real startup.sh actually has the wire-up block we're testing.
#    If startup.sh refactors the block out, these isolation tests stay green
#    but the real startup.sh might regress — this guard ties the test to source.
# ----------------------------------------------------------------------
test_wireup_block_present_in_startup() {
  local startup="$(dirname "$0")/../src/startup.sh"
  if [ ! -f "$startup" ]; then
    echo "  FAIL: src/startup.sh not found at $startup"
    return 1
  fi
  if ! grep -qF 'sutando-shell-setup.sh' "$startup"; then
    echo "  FAIL: src/startup.sh no longer references sutando-shell-setup.sh"
    return 1
  fi
  if ! grep -qE -e '--auto' "$startup"; then
    echo "  FAIL: src/startup.sh no longer calls the helper with --auto"
    return 1
  fi
  if ! grep -qF '|| true' "$startup"; then
    echo "  FAIL: src/startup.sh dropped the '|| true' — failures will abort startup"
    return 1
  fi
  return 0
}

# ----------------------------------------------------------------------
# Run them all
# ----------------------------------------------------------------------
echo "tests/startup-shell-setup-wireup.test.sh — running"
echo

run_test "1. helper absent → driver continues"               test_helper_absent
run_test "2. helper exits non-zero → || true swallows"       test_helper_exits_nonzero
run_test "3. helper exits zero → driver continues"           test_helper_exits_zero
run_test "4. startup.sh actually has the wire-up block"      test_wireup_block_present_in_startup

echo
echo "----------------------------------------"
echo "PASSED: $PASS"
echo "FAILED: $FAIL"
[ "$FAIL" -gt 0 ] && exit 1
exit 0

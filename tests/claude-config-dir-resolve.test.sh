#!/usr/bin/env bash
# Contract test for src/claude_config_dir.sh — pins every rc branch of the
# resolve-or-refuse policy, and that neither launcher keeps a second copy of it.

set -uo pipefail

REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

run_test() {
  local name="$1"; shift
  printf '%-62s' "$name"
  if "$@"; then echo "ok"; PASS=$((PASS + 1)); else echo "FAIL"; FAIL=$((FAIL + 1)); fi
}

# Fake repo carrying only what the helper itself needs to resolve.
setup_repo() {
  local helper_present="$1"   # yes | no
  local subdir="$2"
  SANDBOX="$(mktemp -d -t ccd-resolve.XXXXXX)"
  SANDBOX="$(cd "$SANDBOX" && pwd -P)"
  REPO_FAKE="$SANDBOX/repo"
  mkdir -p "$REPO_FAKE/scripts" "$REPO_FAKE/src" "$SANDBOX/workspace"
  if [ "$helper_present" = "yes" ]; then
    cp "$REAL_REPO/scripts/sutando-config.sh" "$REPO_FAKE/scripts/"
    cp "$REAL_REPO/scripts/python-binary.sh" "$REPO_FAKE/scripts/"
    cp "$REAL_REPO/src/sutando_config.py" "$REPO_FAKE/src/"
    cat > "$REPO_FAKE/sutando.config.json" << EOF
{
  "workspace": {"path": "$SANDBOX/workspace"},
  "claude_sutando_config_dir": {"subdir": "$subdir"}
}
EOF
  fi
}

cleanup_repo() { [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX"; unset SANDBOX REPO_FAKE; }

# Resolve in a subshell so an exported CLAUDE_CONFIG_DIR can't leak between cases.
# Writes stdout to $OUT, stderr to $ERR, returns the function's status.
resolve() {
  local caller_ccd="${1:-}"
  OUT="$SANDBOX/out"; ERR="$SANDBOX/err"
  (
    source "$REAL_REPO/src/claude_config_dir.sh"
    if [ -n "$caller_ccd" ]; then export CLAUDE_CONFIG_DIR="$caller_ccd"; else unset CLAUDE_CONFIG_DIR; fi
    resolve_claude_config_dir "$REPO_FAKE" testcase
  ) > "$OUT" 2> "$ERR"
}

test_valid_config_returns_dir() {
  setup_repo yes ".claude-sutando"
  resolve; rc=$?
  if [ "$rc" != "0" ]; then echo "  FAIL: rc=$rc want 0"; cat "$ERR"; cleanup_repo; return 1; fi
  want="$SANDBOX/workspace/.claude-sutando"
  got="$(cat "$OUT")"
  if [ "$got" != "$want" ]; then echo "  FAIL: got '$got' want '$want'"; cleanup_repo; return 1; fi
  cleanup_repo; return 0
}

# Mutation guard for -x → -r: a helper stripped of its exec bit still runs under
# `bash <file>`, so classifying it absent would refuse a resolvable config.
test_non_executable_helper_still_resolves() {
  setup_repo yes ".claude-sutando"
  chmod -x "$REPO_FAKE/scripts/sutando-config.sh"
  [ -x "$REPO_FAKE/scripts/sutando-config.sh" ] && { echo "  FAIL: chmod -x did not take"; cleanup_repo; return 1; }
  resolve; rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: rc=$rc — a readable, non-executable helper was treated as absent"
    cat "$ERR"; cleanup_repo; return 1
  fi
  if [ "$(cat "$OUT")" != "$SANDBOX/workspace/.claude-sutando" ]; then
    echo "  FAIL: wrong dir '$(cat "$OUT")'"; cleanup_repo; return 1
  fi
  cleanup_repo; return 0
}

test_invalid_config_refuses() {
  setup_repo yes "/etc/claude-state"   # absolute path violates the sub-folder invariant
  resolve; rc=$?
  if [ "$rc" != "1" ]; then echo "  FAIL: rc=$rc want 1"; cleanup_repo; return 1; fi
  if ! grep -q "refusing to start" "$ERR"; then
    echo "  FAIL: no diagnostic on stderr"; cat "$ERR"; cleanup_repo; return 1
  fi
  cleanup_repo; return 0
}

test_absent_helper_honours_caller() {
  setup_repo no "(unused)"
  resolve "$SANDBOX/caller-scoped"; rc=$?
  if [ "$rc" != "2" ]; then echo "  FAIL: rc=$rc want 2"; cat "$ERR"; cleanup_repo; return 1; fi
  if [ "$(cat "$OUT")" != "$SANDBOX/caller-scoped" ]; then
    echo "  FAIL: caller value not echoed back — got '$(cat "$OUT")'"; cleanup_repo; return 1
  fi
  cleanup_repo; return 0
}

test_absent_helper_no_caller_refuses() {
  setup_repo no "(unused)"
  resolve; rc=$?
  if [ "$rc" != "1" ]; then echo "  FAIL: rc=$rc want 1"; cleanup_repo; return 1; fi
  if ! grep -q "refusing to start" "$ERR"; then
    echo "  FAIL: no diagnostic on stderr"; cat "$ERR"; cleanup_repo; return 1
  fi
  cleanup_repo; return 0
}

# Delegation, both launchers: a behavioral test of one cannot see a second copy
# of the policy in the other.
test_both_launchers_delegate() {
  local rc=0 f
  for f in src/agent/claude/cli/start-cli.sh src/startup.sh; do
    if ! grep -qF 'source "$REPO/src/claude_config_dir.sh"' "$REAL_REPO/$f"; then
      echo "  FAIL: $f does not source the shared resolver"; rc=1
    fi
    if ! grep -qF 'resolve_claude_config_dir "$REPO"' "$REAL_REPO/$f"; then
      echo "  FAIL: $f does not call resolve_claude_config_dir"; rc=1
    fi
    # Invocation form only — a prose mention of the subcommand is not a copy.
    if grep -qF 'sutando-config.sh" claude-sutando-config-dir' "$REAL_REPO/$f"; then
      echo "  FAIL: $f still calls the M0 subcommand directly — second copy of the policy"; rc=1
    fi
  done
  return $rc
}

echo "tests/claude-config-dir-resolve.test.sh — running"
echo
run_test "1. helper + valid config → rc 0, dir"              test_valid_config_returns_dir
run_test "2. helper readable but not executable → rc 0"      test_non_executable_helper_still_resolves
run_test "3. helper + invalid config → rc 1, diagnostic"     test_invalid_config_refuses
run_test "4. helper absent + caller env → rc 2, passthrough" test_absent_helper_honours_caller
run_test "5. helper absent + no caller env → rc 1"           test_absent_helper_no_caller_refuses
run_test "6. both launchers delegate, no second copy"        test_both_launchers_delegate
echo
echo "----------------------------------------"
echo "PASSED: $PASS"
echo "FAILED: $FAIL"
[ "$FAIL" -gt 0 ] && exit 1
exit 0

#!/usr/bin/env bash
# Integration test for scripts/start-cli.sh's CLAUDE_CONFIG_DIR export.
#
# Covers the 3 states the design doc enumerated:
#   1. M0 helper missing                              → silent fallback, claude spawns w/o env
#   2. helper present + valid config                  → env exported to claude
#   3. helper present + invariant-violating config    → exit 1, refuse to start
#
# Strategy: stub `claude` to a script that dumps its env and exits 0. Stub
# `tmux` likewise (start-cli uses tmux for the spawn wrapper). Run start-cli
# with a fake repo + stubbed binaries on PATH, then inspect the dumped env.
#
# Each test runs in an isolated sandbox: temp REPO, temp PATH with stubs,
# temp HOME so real ~/.claude state isn't touched.
#
# Run:    bash tests/start-cli-claude-config-dir.test.sh
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

# Sandbox: build a fake repo at $SANDBOX/repo, a fake bin dir with stubbed
# claude + tmux at $SANDBOX/bin, then run start-cli in that environment.
#
# The fake claude stub writes `CLAUDE_CONFIG_DIR=<value>` to $ENV_DUMP so we
# can grep for it post-run. tmux stub just calls through to claude (skipping
# the actual tmux wrapping) so the test stays simple.
setup_sandbox() {
  local helper_present="$1"   # "yes" | "no"
  local helper_subdir="$2"    # subdir to put in fake config; e.g. ".claude-sutando" (valid) or "/etc/claude" (invalid)

  SANDBOX="$(mktemp -d -t start-cli-test.XXXXXX)"
  REPO_FAKE="$SANDBOX/repo"
  ENV_DUMP="$SANDBOX/env-dump"
  BIN_STUB="$SANDBOX/bin"
  export HOME="$SANDBOX/home"
  export SUTANDO_WORKSPACE="$SANDBOX/workspace"

  mkdir -p "$REPO_FAKE/scripts" "$REPO_FAKE/src" "$BIN_STUB" \
           "$HOME" "$SUTANDO_WORKSPACE/state"

  # Stub `claude` binary — records its env to ENV_DUMP, exits 0.
  cat > "$BIN_STUB/claude" << EOF
#!/bin/bash
env > "$ENV_DUMP"
exit 0
EOF
  chmod +x "$BIN_STUB/claude"

  # Stub `pgrep` — start-cli's L34/L48 use pgrep to detect an existing
  # sutando-core session and short-circuit to "already running". The TEST
  # runner itself IS a claude process, so real pgrep would match and the
  # spawn path never runs. Stub returns exit 1 (no matches found).
  cat > "$BIN_STUB/pgrep" << 'EOF'
#!/bin/bash
exit 1
EOF
  chmod +x "$BIN_STUB/pgrep"

  # Stub `tmux` — start-cli sometimes calls into tmux to wrap the claude
  # spawn. Stub it to exec claude directly, bypassing the tmux layer.
  cat > "$BIN_STUB/tmux" << EOF
#!/bin/bash
# Recognize new-session form and run the trailing command (claude). Other
# tmux invocations (start-server, set-option, has-session) are no-ops.
case "\$1" in
  -S)
    shift 2  # drop -S /tmp/socket
    ;;
esac
case "\$1" in
  new-session)
    # find the trailing claude command
    while [ "\$#" -gt 0 ] && [ "\$1" != "claude" ]; do shift; done
    if [ "\$1" = "claude" ]; then exec "\$@"; fi
    ;;
  has-session|kill-session|start-server|set-option|bind|attach)
    :  # no-op
    ;;
esac
exit 0
EOF
  chmod +x "$BIN_STUB/tmux"

  export PATH="$BIN_STUB:$PATH"

  # Copy the real start-cli.sh into the fake repo and the bits it needs to
  # actually resolve claude_sutando_config_dir.
  cp "$REAL_REPO/scripts/start-cli.sh" "$REPO_FAKE/scripts/"

  if [ "$helper_present" = "yes" ]; then
    cp "$REAL_REPO/scripts/sutando-config.sh" "$REPO_FAKE/scripts/"
    cp "$REAL_REPO/src/sutando_config.py" "$REPO_FAKE/src/"
    cat > "$REPO_FAKE/sutando.config.json" << EOF
{
  "workspace": {"path": "\${REPO_DIR}/workspace"},
  "claude_sutando_config_dir": {"subdir": "$helper_subdir"}
}
EOF
  fi
}

cleanup_sandbox() {
  [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX" || true
  unset SANDBOX REPO_FAKE ENV_DUMP BIN_STUB HOME SUTANDO_WORKSPACE
}

REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ----------------------------------------------------------------------
# 1. M0 helper MISSING → silent fallback, start-cli still launches claude,
#    CLAUDE_CONFIG_DIR is NOT set (or set to default from the parent env).
# ----------------------------------------------------------------------
test_helper_missing_silent_fallback() {
  setup_sandbox "no" "(unused)"
  # Run start-cli; should reach claude stub without erroring on missing helper.
  bash "$REPO_FAKE/scripts/start-cli.sh" </dev/null >/dev/null 2>&1
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: start-cli exit $rc (expected 0 — helper-missing should be silent fallback)"
    cleanup_sandbox; return 1
  fi
  if [ ! -f "$ENV_DUMP" ]; then
    echo "  FAIL: claude stub never ran — start-cli didn't reach the spawn"
    cleanup_sandbox; return 1
  fi
  # CLAUDE_CONFIG_DIR should NOT be set by start-cli (the helper-present
  # block was skipped). If the parent env had one, it'd pass through, but
  # we cleared the test env.
  if grep -q "^CLAUDE_CONFIG_DIR=" "$ENV_DUMP"; then
    echo "  FAIL: CLAUDE_CONFIG_DIR was set even though helper is absent"
    grep "^CLAUDE_CONFIG_DIR=" "$ENV_DUMP"
    cleanup_sandbox; return 1
  fi
  cleanup_sandbox
  return 0
}

# ----------------------------------------------------------------------
# 2. helper present + VALID config → CLAUDE_CONFIG_DIR exported, claude gets it.
# ----------------------------------------------------------------------
test_valid_config_exports_env() {
  setup_sandbox "yes" ".claude-sutando"
  bash "$REPO_FAKE/scripts/start-cli.sh" </dev/null >/dev/null 2>&1
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  FAIL: start-cli exit $rc (expected 0 for valid config)"
    cleanup_sandbox; return 1
  fi
  if [ ! -f "$ENV_DUMP" ]; then
    echo "  FAIL: claude stub never ran"
    cleanup_sandbox; return 1
  fi
  ccd_in_env="$(grep "^CLAUDE_CONFIG_DIR=" "$ENV_DUMP" | head -1)"
  if [ -z "$ccd_in_env" ]; then
    echo "  FAIL: CLAUDE_CONFIG_DIR not in claude's env"
    cleanup_sandbox; return 1
  fi
  # Must point at SUTANDO_WORKSPACE/.claude-sutando.
  expected="CLAUDE_CONFIG_DIR=$SUTANDO_WORKSPACE/.claude-sutando"
  if [ "$ccd_in_env" != "$expected" ]; then
    echo "  FAIL: CLAUDE_CONFIG_DIR mismatch"
    echo "    expected : $expected"
    echo "    actual   : $ccd_in_env"
    cleanup_sandbox; return 1
  fi
  cleanup_sandbox
  return 0
}

# ----------------------------------------------------------------------
# 3. helper present + INVARIANT-VIOLATING config (e.g. absolute path) →
#    start-cli refuses, exits 1, claude stub never runs.
# ----------------------------------------------------------------------
test_invalid_config_refuses_to_start() {
  setup_sandbox "yes" "/etc/claude-state"  # absolute path, invariant violation
  bash "$REPO_FAKE/scripts/start-cli.sh" </dev/null >/dev/null 2>&1
  rc=$?
  if [ "$rc" = "0" ]; then
    echo "  FAIL: start-cli exit 0 (expected non-zero — config violates invariant)"
    cleanup_sandbox; return 1
  fi
  if [ -f "$ENV_DUMP" ]; then
    echo "  FAIL: claude stub ran despite config error — start-cli should have refused"
    cleanup_sandbox; return 1
  fi
  cleanup_sandbox
  return 0
}

# ----------------------------------------------------------------------
# 4. Source-tied guard — start-cli.sh actually contains the CLAUDE_CONFIG_DIR
#    block. If a future refactor strips it without intent, the isolation
#    tests above stay green but this one catches the regression.
# ----------------------------------------------------------------------
test_block_present_in_start_cli() {
  if ! grep -qF 'CLAUDE_CONFIG_DIR' "$REAL_REPO/scripts/start-cli.sh"; then
    echo "  FAIL: scripts/start-cli.sh no longer references CLAUDE_CONFIG_DIR"
    return 1
  fi
  if ! grep -qF 'claude-sutando-config-dir' "$REAL_REPO/scripts/start-cli.sh"; then
    echo "  FAIL: scripts/start-cli.sh no longer calls the M0 helper subcommand"
    return 1
  fi
  if ! grep -qF 'refusing to start core' "$REAL_REPO/scripts/start-cli.sh"; then
    echo "  FAIL: scripts/start-cli.sh dropped the fail-loud branch on invariant violation"
    return 1
  fi
  return 0
}

# ----------------------------------------------------------------------
echo "tests/start-cli-claude-config-dir.test.sh — running"
echo

run_test "1. helper missing → silent fallback"              test_helper_missing_silent_fallback
run_test "2. helper + valid config → env exported"          test_valid_config_exports_env
run_test "3. helper + invalid config → refuses to start"    test_invalid_config_refuses_to_start
run_test "4. source-tied guard: block present in script"    test_block_present_in_start_cli

echo
echo "----------------------------------------"
echo "PASSED: $PASS"
echo "FAILED: $FAIL"
[ "$FAIL" -gt 0 ] && exit 1
exit 0

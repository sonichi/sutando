#!/usr/bin/env bash
# Integration test for src/agent/claude/cli/start-cli.sh's CLAUDE_CONFIG_DIR export.
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
  # Resolve symlinks so path comparisons match the helper's resolved output
  # (macOS mktemp returns /var/folders/... which is a symlink to /private/var/...).
  SANDBOX="$(cd "$SANDBOX" && pwd -P)"
  REPO_FAKE="$SANDBOX/repo"
  ENV_DUMP="$SANDBOX/env-dump"
  BIN_STUB="$SANDBOX/bin"
  export HOME="$SANDBOX/home"
  export SUTANDO_WORKSPACE="$SANDBOX/workspace"
  # Prevent ambient CLAUDE_CONFIG_DIR (from the test runner's own session) from
  # leaking into the spawned process and breaking the "not set" assertion.
  unset CLAUDE_CONFIG_DIR

  mkdir -p "$REPO_FAKE/scripts" "$REPO_FAKE/src" "$REPO_FAKE/src/agent/claude/cli" "$REPO_FAKE/hooks" "$BIN_STUB" \
           "$HOME" "$SUTANDO_WORKSPACE/state"

  # Stub `claude` binary — records its env to ENV_DUMP, exits 0.
  cat > "$BIN_STUB/claude" << EOF
#!/bin/bash
env > "$ENV_DUMP"
exit 0
EOF
  chmod +x "$BIN_STUB/claude"

  # Stubs must model the core LIFECYCLE, not a fixed "nothing is running".
  # #2418 added a post-launch liveness verify (tmux_core_session_running: session
  # exists AND a live `claude --name $SESSION` under it) that exits non-zero when
  # the core does not come up. Stubs that always report "not running" satisfy the
  # pre-launch short-circuit but can never satisfy that verify, so start-cli
  # correctly exits 1 and every case here failed on the exit code before reaching
  # its real assertions. A marker file, written by the new-session stub, is the
  # before/after discriminator.
  CORE_UP_MARKER="$SANDBOX/.core-up"
  export CORE_UP_MARKER

  # Stub `pgrep` — before launch: no match (so the "already running"
  # short-circuit is not taken; the test runner is itself a claude process, so
  # real pgrep would match). After launch: report the fake core pid.
  cat > "$BIN_STUB/pgrep" << EOF
#!/bin/bash
[ -f "$CORE_UP_MARKER" ] || exit 1
echo "424242 claude"
EOF
  chmod +x "$BIN_STUB/pgrep"

  # Stub `ps` — core_claude_pids cross-checks each pgrep pid against
  # \\`ps -p <pid> -o args=\\` and keeps only those whose args carry
  # "--name \$SESSION". Faking pgrep alone is not enough.
  cat > "$BIN_STUB/ps" << EOF
#!/bin/bash
case " \$* " in
  *" 424242 "*)
    [ -f "$CORE_UP_MARKER" ] || exit 1
    echo "claude --name sutando-core --dangerously-skip-permissions"
    ;;
  *) exit 1 ;;
esac
EOF
  chmod +x "$BIN_STUB/ps"

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
    # The core is now "up" for every later liveness probe in this run.
    touch "$CORE_UP_MARKER"
    # find the trailing claude command
    while [ "\$#" -gt 0 ] && [ "\$1" != "claude" ]; do shift; done
    if [ "\$1" = "claude" ]; then exec "\$@"; fi
    ;;
  has-session)
    # Before new-session the sandbox has no managed session (returning success
    # here would send the launcher down its orphaned-session healing path
    # instead of the new-session path this test exercises). After new-session
    # it must report the session so #2418's post-launch verify can pass.
    [ -f "$CORE_UP_MARKER" ] || exit 1
    ;;
  kill-session|start-server|set-option|bind|attach)
    :  # no-op
    ;;
esac
exit 0
EOF
  chmod +x "$BIN_STUB/tmux"

  export PATH="$BIN_STUB:$PATH"

  # Copy the real start-cli.sh into the fake repo (mirroring its real
  # src/agent/claude/cli/ location, since the script self-locates the repo root
  # four levels up: cli → claude → agent → src → repo) plus the bits it needs to
  # resolve claude_sutando_config_dir
  # (sutando-config.sh stays under scripts/ — start-cli calls $REPO/scripts/...).
  cp "$REAL_REPO/src/agent/claude/cli/start-cli.sh" "$REPO_FAKE/src/agent/claude/cli/"
  # start-cli sources the shared resolve-or-refuse policy from $REPO/src/.
  cp "$REAL_REPO/src/claude_config_dir.sh" "$REPO_FAKE/src/"
  # Sourced by the launcher before anything else it does here.
  cp "$REAL_REPO/src/agent/restart-guard.sh" "$REPO_FAKE/src/agent/"
  cp "$REAL_REPO/src/agent/claude/cli/build-core-settings.mjs" "$REPO_FAKE/src/agent/claude/cli/"
  cp "$REAL_REPO/hooks/skip-ask-user-question.py" "$REPO_FAKE/hooks/"

  if [ "$helper_present" = "yes" ]; then
    cp "$REAL_REPO/scripts/sutando-config.sh" "$REPO_FAKE/scripts/"
    # sutando-config.sh sources this; without it the fake repo dies with
    # "python-binary.sh: No such file or directory" (CI, #2599).
    cp "$REAL_REPO/scripts/python-binary.sh" "$REPO_FAKE/scripts/"
    cp "$REAL_REPO/src/sutando_config.py" "$REPO_FAKE/src/"
    # Use absolute path so workspace resolves to $SANDBOX/workspace (the same
    # dir the mkdir above created), avoiding a mismatch with ${REPO_DIR}/workspace.
    cat > "$REPO_FAKE/sutando.config.json" << EOF
{
  "workspace": {"path": "$SANDBOX/workspace"},
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
# 1. M0 helper MISSING → refuse to start. An unset CLAUDE_CONFIG_DIR selects
#    ~/.claude — a different credential store, surfacing as a 401 loop.
# ----------------------------------------------------------------------
test_helper_missing_refuses_to_start() {
  setup_sandbox "no" "(unused)"
  err_log="$SANDBOX/helper-missing.err"
  bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>"$err_log"
  rc=$?
  if [ "$rc" = "0" ]; then
    echo "  FAIL: start-cli exit 0 — it fell back silently instead of refusing"
    cleanup_sandbox; return 1
  fi
  # The spawn must not happen: a core that started is a core reading the wrong
  # credential store, which is the whole failure this refuses to produce.
  if [ -f "$ENV_DUMP" ]; then
    echo "  FAIL: claude was spawned anyway (env dump exists) — refusal did not gate the spawn"
    cleanup_sandbox; return 1
  fi
  # Refusing silently would be its own trap: the operator needs to know why.
  if ! grep -q "sutando-config.sh missing or unreadable" "$err_log"; then
    echo "  FAIL: refusal message absent from stderr — operator gets an unexplained exit"
    cat "$err_log"
    cleanup_sandbox; return 1
  fi
  cleanup_sandbox
  return 0
}

# ----------------------------------------------------------------------
# 1b. M0 helper MISSING but the CALLER exported CLAUDE_CONFIG_DIR → start and
#     pass it through: that value already reaches the right credential store.
# ----------------------------------------------------------------------
test_helper_missing_honours_caller_config_dir() {
  setup_sandbox "no" "(unused)"
  caller_ccd="$SANDBOX/caller-scoped-config"
  export CLAUDE_CONFIG_DIR="$caller_ccd"
  bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
  rc=$?
  unset CLAUDE_CONFIG_DIR
  if [ "$rc" != "0" ]; then
    echo "  FAIL: start-cli exit $rc — refused despite a caller-provided CLAUDE_CONFIG_DIR"
    cleanup_sandbox; return 1
  fi
  if [ ! -f "$ENV_DUMP" ]; then
    echo "  FAIL: claude stub never ran"
    cleanup_sandbox; return 1
  fi
  got="$(grep '^CLAUDE_CONFIG_DIR=' "$ENV_DUMP" | head -1)"
  if [ "$got" != "CLAUDE_CONFIG_DIR=$caller_ccd" ]; then
    echo "  FAIL: caller value not passed through — got '${got:-<unset>}', want 'CLAUDE_CONFIG_DIR=$caller_ccd'"
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
  bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
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
  # Must point at <workspace>/.claude-sutando where <workspace> is resolved
  # from sutando.config.json (= "${REPO_DIR}/workspace" = $REPO_FAKE/workspace).
  # $SUTANDO_WORKSPACE is the deprecated v0.8 env-var and is no longer read by
  # the resolver — using it here would give a stale path on a clean CI runner.
  expected="CLAUDE_CONFIG_DIR=$SANDBOX/workspace/.claude-sutando"
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
  bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
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
  if ! grep -qF 'CLAUDE_CONFIG_DIR' "$REAL_REPO/src/agent/claude/cli/start-cli.sh"; then
    echo "  FAIL: src/agent/claude/cli/start-cli.sh no longer references CLAUDE_CONFIG_DIR"
    return 1
  fi
  if ! grep -qF 'resolve_claude_config_dir "$REPO"' "$REAL_REPO/src/agent/claude/cli/start-cli.sh"; then
    echo "  FAIL: src/agent/claude/cli/start-cli.sh no longer delegates to the shared resolver"
    return 1
  fi
  if ! grep -qF 'refusing to start' "$REAL_REPO/src/claude_config_dir.sh"; then
    echo "  FAIL: src/claude_config_dir.sh dropped the fail-loud branch"
    return 1
  fi
  return 0
}

# ----------------------------------------------------------------------
echo "tests/start-cli-claude-config-dir.test.sh — running"
echo

run_test "1. helper missing → refuses to start"              test_helper_missing_refuses_to_start
run_test "1b. helper missing + caller-set dir → honoured"     test_helper_missing_honours_caller_config_dir
run_test "2. helper + valid config → env exported"          test_valid_config_exports_env
run_test "3. helper + invalid config → refuses to start"    test_invalid_config_refuses_to_start
run_test "4. source-tied guard: delegation present in script" test_block_present_in_start_cli

echo
echo "----------------------------------------"
echo "PASSED: $PASS"
echo "FAILED: $FAIL"
[ "$FAIL" -gt 0 ] && exit 1
exit 0

#!/usr/bin/env bash
# Tests for src/agent/claude/cli/start-cli.sh's provider-backed core mode
# (core_config.provider) + the shared src/agent/claude/provider-env.sh helper.
#
# When core_config.provider is set, start-cli.sh boots the SAME interactive core
# but pointed at a custom Anthropic-compatible provider: it sources provider-env.sh
# to export ANTHROPIC_BASE_URL / <auth_env> token / ANTHROPIC_MODEL, and suppresses
# the subscription-only SUTANDO_CORE_MODEL → --model pin. With no provider set it
# runs on the Claude.ai subscription exactly as before.
#
# Strategy (same as start-cli-claude-config-dir.test.sh): build a fake repo with
# the real start-cli.sh + provider-env.sh + sutando-config.sh + sutando_config.py,
# stub claude/tmux/pgrep on PATH, run start-cli, inspect the env the claude stub saw.
#
# Run:    bash tests/start-cli-provider-as-core.test.sh
# Exit:   0 on pass, 1 on first failure.

set -uo pipefail

PASS=0
FAIL=0
run_test() {
  local name="$1"; shift
  printf '%-58s' "$name"
  if "$@"; then echo "ok"; PASS=$((PASS + 1)); else echo "FAIL"; FAIL=$((FAIL + 1)); fi
}

REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"

# $1 = provider URL (empty string = subscription core)
# $2 = auth_env (optional; default ANTHROPIC_AUTH_TOKEN)
setup_sandbox() {
  local provider="$1"
  local auth_env="${2:-ANTHROPIC_AUTH_TOKEN}"
  SANDBOX="$(mktemp -d -t start-cli-provider.XXXXXX)"
  REPO_FAKE="$SANDBOX/repo"
  ENV_DUMP="$SANDBOX/env-dump"
  BIN_STUB="$SANDBOX/bin"
  export HOME="$SANDBOX/home"

  mkdir -p "$REPO_FAKE/src/agent/claude/cli" "$REPO_FAKE/scripts" "$REPO_FAKE/src" \
           "$BIN_STUB" "$HOME" "$REPO_FAKE/workspace/state"

  # start-cli.sh lives in cli/ (self-locates 4 levels up); provider-env.sh is
  # shared and stays at the claude/ root (start-cli sources it via $REPO/...).
  cp "$REAL_REPO/src/agent/claude/cli/start-cli.sh"   "$REPO_FAKE/src/agent/claude/cli/"
  cp "$REAL_REPO/src/agent/claude/provider-env.sh"    "$REPO_FAKE/src/agent/claude/"
  cp "$REAL_REPO/scripts/sutando-config.sh"           "$REPO_FAKE/scripts/"
  cp "$REAL_REPO/src/sutando_config.py"               "$REPO_FAKE/src/"

  cat > "$REPO_FAKE/sutando.config.json" << EOF
{
  "workspace": { "path": "\${REPO_DIR}/workspace" },
  "core_config": {
    "core_type": "claude_cli",
    "provider": "$provider",
    "auth_env": "$auth_env",
    "model": "prov-model-7"
  }
}
EOF

  # claude stub — dumps the provider-relevant env + argv, exits 0.
  cat > "$BIN_STUB/claude" << EOF
#!/bin/bash
{
  echo "ARGV=\$*"
  echo "BASE_URL=\${ANTHROPIC_BASE_URL:-}"
  echo "MODEL=\${ANTHROPIC_MODEL:-}"
  echo "TOKEN=\${ANTHROPIC_AUTH_TOKEN:-}"
} > "$ENV_DUMP"
exit 0
EOF
  chmod +x "$BIN_STUB/claude"

  # tmux stub — run the trailing claude command, no-op everything else.
  cat > "$BIN_STUB/tmux" << 'EOF'
#!/bin/bash
case "$1" in -S) shift 2 ;; esac
case "$1" in
  new-session) while [ "$#" -gt 0 ] && [ "$1" != "claude" ]; do shift; done; [ "$1" = "claude" ] && exec "$@" ;;
  *) : ;;
esac
exit 0
EOF
  chmod +x "$BIN_STUB/tmux"
  printf '#!/bin/bash\nexit 1\n' > "$BIN_STUB/pgrep"; chmod +x "$BIN_STUB/pgrep"

  export PATH="$BIN_STUB:$PATH"
}

cleanup() { [ -n "${SANDBOX:-}" ] && rm -rf "$SANDBOX" || true; unset SANDBOX REPO_FAKE ENV_DUMP BIN_STUB HOME; }

PROVIDER="https://prov.example.com"

# 1. provider set + token in env → provider env reaches claude; the
#    SUTANDO_CORE_MODEL pin does NOT become a --model flag (provider owns the model).
test_provider_with_token() {
  setup_sandbox "$PROVIDER"
  ANTHROPIC_AUTH_TOKEN="prov-tok" SUTANDO_CORE_MODEL="opus" \
    bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
  local rc=$?
  [ "$rc" = "0" ] || { echo "  FAIL: start-cli exit $rc (expected 0)"; cleanup; return 1; }
  [ -f "$ENV_DUMP" ] || { echo "  FAIL: claude stub never ran"; cleanup; return 1; }
  grep -q "^BASE_URL=$PROVIDER$" "$ENV_DUMP" || { echo "  FAIL: ANTHROPIC_BASE_URL not exported"; cat "$ENV_DUMP"; cleanup; return 1; }
  grep -q "^MODEL=prov-model-7$" "$ENV_DUMP" || { echo "  FAIL: ANTHROPIC_MODEL not exported"; cat "$ENV_DUMP"; cleanup; return 1; }
  grep -q "^TOKEN=prov-tok$" "$ENV_DUMP" || { echo "  FAIL: token not exported"; cleanup; return 1; }
  if grep -q "^ARGV=.*--model opus" "$ENV_DUMP"; then
    echo "  FAIL: --model opus leaked into provider-mode launch"; cat "$ENV_DUMP"; cleanup; return 1
  fi
  # sanity: it really is the interactive core launch
  grep -q "^ARGV=.*--name sutando-core" "$ENV_DUMP" || { echo "  FAIL: not the interactive core launch"; cleanup; return 1; }
  cleanup; return 0
}

# 1b. provider set + ANTHROPIC_BASE_URL pre-set to the credential proxy (as
#     src/startup.sh does) → the config provider MUST win, not the proxy.
test_provider_sheds_proxy_base_url() {
  setup_sandbox "$PROVIDER"
  ANTHROPIC_AUTH_TOKEN="prov-tok" ANTHROPIC_BASE_URL="http://localhost:7846" \
    bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
  [ -f "$ENV_DUMP" ] || { echo "  FAIL: claude stub never ran"; cleanup; return 1; }
  grep -q "^BASE_URL=$PROVIDER$" "$ENV_DUMP" || { echo "  FAIL: proxy base_url leaked (expected provider url)"; cat "$ENV_DUMP"; cleanup; return 1; }
  cleanup; return 0
}

# 2. provider set + NO token → refuse to start (exit 1), claude never runs.
test_provider_no_token_refuses() {
  setup_sandbox "$PROVIDER"
  # scrub any inherited token
  unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY
  bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >"$SANDBOX/log" 2>&1
  local rc=$?
  [ "$rc" != "0" ] || { echo "  FAIL: start-cli exit 0 (expected non-zero refusal)"; cleanup; return 1; }
  [ ! -f "$ENV_DUMP" ] || { echo "  FAIL: claude ran despite missing token"; cleanup; return 1; }
  grep -qi "refusing to start core" "$SANDBOX/log" || { echo "  FAIL: no refusal message"; cat "$SANDBOX/log"; cleanup; return 1; }
  cleanup; return 0
}

# 2b. provider set + auth_env=ANTHROPIC_SUBSCRIPTION → contradictory (a provider
#     needs a token) → refuse to start, even if a token is present in the env.
test_provider_subscription_auth_refuses() {
  setup_sandbox "$PROVIDER" "ANTHROPIC_SUBSCRIPTION"
  ANTHROPIC_AUTH_TOKEN="prov-tok" \
    bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >"$SANDBOX/log" 2>&1
  local rc=$?
  [ "$rc" != "0" ] || { echo "  FAIL: start-cli exit 0 (expected refusal for provider+subscription auth)"; cleanup; return 1; }
  [ ! -f "$ENV_DUMP" ] || { echo "  FAIL: claude ran despite provider+subscription misconfig"; cleanup; return 1; }
  grep -qi "ANTHROPIC_SUBSCRIPTION" "$SANDBOX/log" || { echo "  FAIL: refusal didn't mention the subscription-auth misconfig"; cat "$SANDBOX/log"; cleanup; return 1; }
  cleanup; return 0
}

# 3. no provider → subscription path unchanged: no provider env, and the
#    SUTANDO_CORE_MODEL pin IS honored as --model. A stray token in the env must
#    NOT be exported into the subscription launch.
test_no_provider_subscription_unchanged() {
  setup_sandbox ""
  ANTHROPIC_AUTH_TOKEN="stray-tok" SUTANDO_CORE_MODEL="opus" \
    bash "$REPO_FAKE/src/agent/claude/cli/start-cli.sh" </dev/null >/dev/null 2>&1
  local rc=$?
  [ "$rc" = "0" ] || { echo "  FAIL: start-cli exit $rc (expected 0)"; cleanup; return 1; }
  [ -f "$ENV_DUMP" ] || { echo "  FAIL: claude stub never ran"; cleanup; return 1; }
  grep -q "^BASE_URL=$" "$ENV_DUMP" || { echo "  FAIL: ANTHROPIC_BASE_URL set when no provider"; cat "$ENV_DUMP"; cleanup; return 1; }
  grep -q "^ARGV=.*--model opus" "$ENV_DUMP" || { echo "  FAIL: --model opus missing on subscription path"; cat "$ENV_DUMP"; cleanup; return 1; }
  cleanup; return 0
}

# 4. Source-tied guard — start-cli.sh actually wires the provider branch + helper.
test_source_guard() {
  local s="$REAL_REPO/src/agent/claude/cli/start-cli.sh"
  grep -qF 'core-config' "$s" || { echo "  FAIL: start-cli.sh no longer reads the core-config getter"; return 1; }
  grep -qF 'provider-env.sh' "$s" || { echo "  FAIL: start-cli.sh no longer sources provider-env.sh"; return 1; }
  grep -qF 'claude_provider_export_env' "$s" || { echo "  FAIL: start-cli.sh no longer calls claude_provider_export_env"; return 1; }
  [ -f "$REAL_REPO/src/agent/claude/provider-env.sh" ] || { echo "  FAIL: provider-env.sh missing"; return 1; }
  return 0
}

echo "tests/start-cli-provider-as-core.test.sh — running"
echo
run_test "1. provider + token → provider env, no model pin" test_provider_with_token
run_test "1b. provider set sheds inherited proxy base_url"  test_provider_sheds_proxy_base_url
run_test "2. provider + no token → refuse to start"         test_provider_no_token_refuses
run_test "2b. provider + subscription auth → refuse"        test_provider_subscription_auth_refuses
run_test "3. no provider → subscription path unchanged"     test_no_provider_subscription_unchanged
run_test "4. source-tied guard: provider branch present"    test_source_guard
echo
echo "----------------------------------------"
echo "PASSED: $PASS"
echo "FAILED: $FAIL"
[ "$FAIL" -gt 0 ] && exit 1
exit 0

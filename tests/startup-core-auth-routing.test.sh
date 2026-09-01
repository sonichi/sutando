#!/bin/bash
# Runtime-specific startup auth routing: Codex must never consult Claude auth.
set -u

REPO_REAL="${REPO_UNDER_TEST:-$(cd "$(dirname "$0")/.." && pwd)}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"
mkdir -p "$REPO/src" "$REPO/scripts" "$TMP/bin" "$TMP/home"
cp "$REPO_REAL/src/startup-runtime.sh" "$REPO/src/startup-runtime.sh"
# startup-runtime.sh sources its sibling resolver; a staged repo needs both.
cp "$REPO_REAL/src/repo_root.sh" "$REPO/src/repo_root.sh"

cat > "$REPO/scripts/sutando-config.sh" <<'SH'
#!/bin/bash
case "$1" in
  core-runtime) printf '%s' "${SUTANDO_CORE_RUNTIME:-claude}" ;;
  core-config-dir-env-name) printf 'CODEX_HOME' ;;
  core-config-dir-value) printf '%s' "$TEST_CODEX_HOME" ;;
  *) exit 2 ;;
esac
SH
chmod +x "$REPO/scripts/sutando-config.sh"

cat > "$REPO/src/auth-preflight-gate.sh" <<'SH'
#!/bin/bash
printf 'claude:%s\n' "$1" >> "$TEST_CALLS"
exit "${TEST_CLAUDE_RC:-0}"
SH
chmod +x "$REPO/src/auth-preflight-gate.sh"

cat > "$TMP/bin/codex" <<'SH'
#!/bin/bash
printf 'codex:%s:CODEX_HOME=%s\n' "$*" "${CODEX_HOME:-}" >> "$TEST_CALLS"
exit "${TEST_CODEX_RC:-0}"
SH
chmod +x "$TMP/bin/codex"

# shellcheck source=../src/startup-runtime.sh
source "$REPO/src/startup-runtime.sh"

pass=0
fail=0
ok() { echo "  ok  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL $1: $2"; fail=$((fail+1)); }
check() {
  if [ "$2" = "$3" ]; then
    ok "$1"
  else
    bad "$1" "expected '$3', got '$2'"
  fi
}

export TEST_CALLS="$TMP/calls" TEST_CODEX_HOME="$TMP/codex-home"
export PATH="$TMP/bin:/usr/bin:/bin"

# .env participates in the early selector without leaking its other values.
printf 'SUTANDO_CORE_RUNTIME=codex\nUNRELATED_SECRET=must-not-leak\n' > "$REPO/.env"
unset SUTANDO_CORE_RUNTIME UNRELATED_SECRET
check ".env runtime override is visible to early selection" "$(resolve_startup_core_runtime)" "codex"
check "early selection does not leak unrelated .env values" "${UNRELATED_SECRET:-unset}" "unset"

# Codex configures the exact home and checks only Codex auth.
: > "$TEST_CALLS"
TEST_CODEX_RC=0 preflight_selected_core_auth codex "$TMP/claude-home"
rc=$?
check "authenticated Codex preflight passes" "$rc" "0"
calls="$(cat "$TEST_CALLS")"
case "$calls" in
  *"codex:login status:CODEX_HOME=$TEST_CODEX_HOME"*) ok "Codex status uses configured CODEX_HOME" ;;
  *) bad "Codex status uses configured CODEX_HOME" "$calls" ;;
esac
case "$calls" in
  *claude:*) bad "Codex does not call Claude auth gate" "$calls" ;;
  *) ok "Codex does not call Claude auth gate" ;;
esac

# A logged-out Codex fails before startup may launch services.
: > "$TEST_CALLS"
TEST_CODEX_RC=1 preflight_selected_core_auth codex "$TMP/claude-home" >"$TMP/out" 2>"$TMP/err"
rc=$?
check "logged-out Codex fails closed" "$rc" "1"
if grep -q "codex login" "$TMP/err"; then
  ok "logged-out Codex names its own remedy"
else
  bad "logged-out Codex names its own remedy" "$(cat "$TMP/err")"
fi

# The one-run escape hatch skips execution but still exports configured home.
: > "$TEST_CALLS"
SUTANDO_SKIP_AUTH_PREFLIGHT=1 preflight_selected_core_auth codex "$TMP/claude-home" >"$TMP/out" 2>"$TMP/err"
rc=$?
check "Codex skip hatch passes" "$rc" "0"
check "Codex skip hatch executes no CLI" "$(cat "$TEST_CALLS")" ""

# Claude behavior is unchanged: the existing rich gate remains authoritative.
: > "$TEST_CALLS"
TEST_CLAUDE_RC=0 preflight_selected_core_auth claude "$TMP/claude-home"
rc=$?
check "Claude preflight still passes through" "$rc" "0"
check "Claude preflight still receives CLAUDE_CONFIG_DIR" "$(cat "$TEST_CALLS")" "claude:$TMP/claude-home"
check "Codex disables Claude credential carry" "$(claude_auth_carry_enabled codex; echo $?)" "1"
check "Claude keeps credential carry" "$(claude_auth_carry_enabled claude; echo $?)" "0"

# Activated startup wiring: select before carry, gate the carry, and dispatch
# before the first service/bootstrap step.
startup="$REPO_REAL/src/startup.sh"
select_line="$(grep -n 'core_runtime="$(resolve_startup_core_runtime)"' "$startup" | cut -d: -f1)"
carry_line="$(grep -n 'if claude_auth_carry_enabled "$core_runtime"' "$startup" | cut -d: -f1)"
gate_line="$(grep -n 'preflight_selected_core_auth "$core_runtime"' "$startup" | cut -d: -f1)"
init_line="$(grep -n 'bash "$REPO/src/init.sh" --auto' "$startup" | cut -d: -f1 | head -1)"
if [ -n "$select_line" ] && [ -n "$carry_line" ] && [ "$select_line" -lt "$carry_line" ]; then
  ok "runtime selection precedes credential carry"
else
  bad "runtime selection precedes credential carry" "select=$select_line carry=$carry_line"
fi
if [ -n "$gate_line" ] && [ -n "$init_line" ] && [ "$gate_line" -lt "$init_line" ]; then
  ok "selected auth gate precedes service bootstrap"
else
  bad "selected auth gate precedes service bootstrap" "gate=$gate_line init=$init_line"
fi

echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]

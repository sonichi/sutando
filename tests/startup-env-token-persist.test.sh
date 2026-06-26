#!/bin/bash
# tests/startup-env-token-persist.test.sh — E2E smoke for Issue #1499 fix
# (Option A: persist env-token to .credentials.json on startup, default-on
# with SUTANDO_NO_PERSIST_TOKEN=1 opt-out).
#
# Tests the env-token-persist block in src/startup.sh that fires after the
# auth-carry. Runs the bash logic in isolation (extracted, not the full
# startup.sh, so we don't need to mock the rest of the script).
#
# Run: bash tests/startup-env-token-persist.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# The block under test (mirrors src/startup.sh ~L83-110 exactly).
run_persist_block() {
  local _ccd="$1"
  if [ ! -f "$_ccd/.credentials.json" ] && [ "${SUTANDO_NO_PERSIST_TOKEN:-0}" != "1" ]; then
    local _env_token=""
    local _env_var_used=""
    for _var in CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN; do
      eval "_val=\${$_var:-}"
      if [ -n "$_val" ]; then
        _env_token="$_val"
        _env_var_used="$_var"
        break
      fi
    done
    if [ -n "$_env_token" ]; then
      if _p="$_ccd/.credentials.json" _t="$_env_token" python3 -c "
import json,os
p=os.environ['_p']
t=os.environ['_t']
json.dump({'claudeAiOauth':{'accessToken':t}}, open(p,'w'))
" 2>/dev/null; then
        chmod 600 "$_ccd/.credentials.json"
        echo "  ~ env-token-persist: wrote .credentials.json from \$$_env_var_used (mode 600)"
        return 0
      else
        echo "  ~ env-token-persist: write failed"
        return 1
      fi
    fi
  fi
  return 0
}

# Test 1: persist fires when env-token set + file absent
T="$(mktemp -d)"
CLAUDE_CODE_OAUTH_TOKEN="test-token-001" run_persist_block "$T" >/dev/null 2>&1
[ -f "$T/.credentials.json" ] && [ "$(jq -r .claudeAiOauth.accessToken "$T/.credentials.json")" = "test-token-001" ]
report "$?" "persist fires + writes correct token from CLAUDE_CODE_OAUTH_TOKEN"

# Test 2: mode 600 enforced
[ "$(stat -f '%Lp' "$T/.credentials.json")" = "600" ]
report "$?" "persisted .credentials.json is mode 600"

# Test 3: schema is the expected claudeAiOauth shape
jq -e '.claudeAiOauth.accessToken' "$T/.credentials.json" >/dev/null 2>&1
report "$?" "persisted file has claudeAiOauth.accessToken structure"

# Test 4: idempotent — re-run with file present should NOT overwrite
old_mtime="$(stat -f '%m' "$T/.credentials.json")"
sleep 1
CLAUDE_CODE_OAUTH_TOKEN="test-token-002" run_persist_block "$T" >/dev/null 2>&1
new_mtime="$(stat -f '%m' "$T/.credentials.json")"
[ "$old_mtime" = "$new_mtime" ] && [ "$(jq -r .claudeAiOauth.accessToken "$T/.credentials.json")" = "test-token-001" ]
report "$?" "idempotent — re-run preserves original token, no overwrite"

# Test 5: opt-out via SUTANDO_NO_PERSIST_TOKEN=1
T2="$(mktemp -d)"
SUTANDO_NO_PERSIST_TOKEN=1 CLAUDE_CODE_OAUTH_TOKEN="test-token-003" run_persist_block "$T2" >/dev/null 2>&1
[ ! -f "$T2/.credentials.json" ]
report "$?" "SUTANDO_NO_PERSIST_TOKEN=1 opt-out honored (no file written)"

# Test 6: no env-token set → skip silently, no file
T3="$(mktemp -d)"
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN
run_persist_block "$T3" >/dev/null 2>&1
[ ! -f "$T3/.credentials.json" ]
report "$?" "no env-token set → skip cleanly, no file written"

# Test 7: ANTHROPIC_AUTH_TOKEN fallback when CLAUDE_CODE_OAUTH_TOKEN absent
T4="$(mktemp -d)"
unset CLAUDE_CODE_OAUTH_TOKEN
ANTHROPIC_AUTH_TOKEN="anthropic-tok-004" run_persist_block "$T4" >/dev/null 2>&1
[ -f "$T4/.credentials.json" ] && [ "$(jq -r .claudeAiOauth.accessToken "$T4/.credentials.json")" = "anthropic-tok-004" ]
report "$?" "ANTHROPIC_AUTH_TOKEN fallback works when CLAUDE_CODE_OAUTH_TOKEN unset"

rm -rf "$T" "$T2" "$T3" "$T4"
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" = "0" ]

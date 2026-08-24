#!/bin/bash
# The gateway wrapper's token gate must be as wide as the bridge it gates.
#
# remote_gateway_bridge resolves env -> .env -> VAULT
# (_token_from_vault_ag2space, reusing channel_token.token_from_vault), so an
# .env-only gate parks a vault-only host before the bridge can resolve anything.
# That is the #3325 defect in the second wrapper.
#
# Runs against a TEMP repo skeleton, never the real checkout: on a passing gate
# the wrapper reaches evict_own_bridge and would kill this host's live bridge.
set -uo pipefail
REAL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fails=0
say() { [ "$1" = ok ] && echo "  ok  $2" || { echo "  FAIL: $2"; fails=$((fails+1)); }; }

REPO="$TMP/repo"
mkdir -p "$REPO/src/launchd" "$REPO/scripts" "$TMP/bin"
cp "$REAL_REPO/src/launchd/gateway-bridge-wrapper.sh" "$REPO/src/launchd/"
# The wrapper resolves its interpreter through this, so the skeleton needs it.
cp "$REAL_REPO/scripts/python-binary.sh" "$REPO/scripts/"
ENVF="$TMP/ag2space.env"

cat > "$REPO/scripts/sutando-config.sh" <<CFG
#!/bin/bash
echo "$ENVF"
CFG
chmod +x "$REPO/scripts/sutando-config.sh"

# stub python3: logs argv, exit code chosen by MODE. Also stands in for the
# bridge exec, so a passing gate cannot launch anything real.
cat > "$TMP/bin/python3" <<PY
#!/bin/bash
printf '%s\n' "\$*" >> "$TMP/argv.log"
case "\${MODE:-absent}" in
  vault)  exit 0 ;;
  alias)  case "\$*" in
            *channel_token.py*REMOTE_TASK_TOKEN*) exit 3 ;;
            *channel_token.py*AG2_REMOTE_TOKEN*)  exit 0 ;;
            *) exit 0 ;;
          esac ;;
  absent) case "\$*" in *channel_token.py*) exit 3 ;; *) exit 0 ;; esac ;;
esac
PY
chmod +x "$TMP/bin/python3"

# env -i, ALWAYS. The wrapper sources a credential file and exports the token;
# an inherited REMOTE_TASK_TOKEN from the operator's shell both defeats every
# case below and can leak a real secret into test output.
run() {  # run(mode, envfile-contents) -> rc in $RC, stderr in $TMP/err
  : > "$TMP/argv.log"; printf '%s' "$2" > "$ENVF"
  env -i HOME="$TMP" MODE="$1" PATH="$TMP/bin:/usr/bin:/bin" \
    SUTANDO_PY="$TMP/bin/python3" \
    bash "$REPO/src/launchd/gateway-bridge-wrapper.sh" > "$TMP/out" 2> "$TMP/err"
  RC=$?
}

# Prove the isolation before relying on it: a poisoned ambient value must NOT
# reach the wrapper, or every "vault-only" case below is vacuous.
REMOTE_TASK_TOKEN="ambient-must-not-leak" run absent ""
if ! grep -q 'no REMOTE_TASK_TOKEN configured' "$TMP/err"; then
  say FAIL "env -i did not isolate the wrapper — an ambient token reached it"
else
  say ok "precondition: ambient REMOTE_TASK_TOKEN cannot reach the wrapper"
fi

echo "1. token ONLY in the vault: the gate must NOT park the job"
run vault ""
if [ "$RC" -ne 0 ]; then
  say FAIL "wrapper exited $RC on a vault-only token"
elif grep -q 'no REMOTE_TASK_TOKEN configured' "$TMP/err"; then
  say FAIL "parked with 'nothing to run' despite a vault token (the pre-fix defect)"
elif ! grep -q 'channel_token.py' "$TMP/argv.log"; then
  say FAIL "the shared resolver was never consulted"
else
  say ok "vault-only token passes the gate via the shared resolver"
fi

echo "2. CONTROL — token nowhere: the gate must still park cleanly (exit 0)"
run absent ""
if [ "$RC" -ne 0 ]; then
  say FAIL "expected a clean exit 0, got $RC"
elif ! grep -q 'no REMOTE_TASK_TOKEN configured' "$TMP/err"; then
  say FAIL "parked without saying why"
else
  say ok "absent token still parks cleanly, and says so"
fi

echo "3. CONTROL — token in .env: unchanged, and the resolver is not needed"
run absent 'REMOTE_TASK_TOKEN=tok-from-envfile'
if [ "$RC" -ne 0 ]; then
  say FAIL "an .env token must still start the bridge (rc=$RC)"
elif grep -q 'no REMOTE_TASK_TOKEN configured' "$TMP/err"; then
  say FAIL "parked despite an .env token — regression in the pre-existing path"
elif grep -q 'channel_token.py' "$TMP/argv.log"; then
  say FAIL "resolver invoked even though .env already answered"
else
  say ok ".env token still works and short-circuits the resolver"
fi

echo "4. ONLY the legacy AG2_REMOTE_TOKEN is in the vault: must still start"
# Discriminating by construction: the resolver answers 3 (absent) for
# REMOTE_TASK_TOKEN and 0 only for AG2_REMOTE_TOKEN, so a gate that dropped the
# legacy alias from its loop parks here. The previous form asserted only that
# SOME var was queried, which the always-first REMOTE_TASK_TOKEN satisfied.
run alias ""
if [ "$RC" -ne 0 ]; then
  say FAIL "a legacy-alias-only vault token must start the bridge (rc=$RC)"
elif grep -q 'no REMOTE_TASK_TOKEN configured' "$TMP/err"; then
  say FAIL "parked despite a vault AG2_REMOTE_TOKEN — legacy alias not consulted"
elif ! grep -q 'AG2_REMOTE_TOKEN' "$TMP/argv.log"; then
  say FAIL "the legacy alias was never queried"
else
  say ok "legacy AG2_REMOTE_TOKEN alone starts the bridge"
fi

[ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILURE(S)"; exit 1; }

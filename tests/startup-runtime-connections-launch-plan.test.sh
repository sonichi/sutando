#!/usr/bin/env bash
# start_gateway_lanes imports env tokens once, then asks the connections CLI to
# launch one bridge per launch-plan line, and never composes a bridge env itself.
# Python is a stand-in that records what it was asked.
set -uo pipefail

REPO_UNDER_TEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pass=0; fail=0
ok()  { pass=$((pass+1)); echo "  ok   $1"; }
bad() { fail=$((fail+1)); echo "  FAIL $1"; }

BOX="$(mktemp -d)"
trap 'rm -rf "$BOX"' EXIT
REPO="$BOX/repo"; OUT="$BOX/out"; LOGS_DIR="$BOX/logs"
mkdir -p "$REPO/src" "$REPO/scripts" "$OUT" "$LOGS_DIR"
cp "$REPO_UNDER_TEST/src/repo_root.sh" "$REPO_UNDER_TEST/src/startup-runtime.sh" "$REPO/src/"
# No launchd installer staged, so the primary takes the bare-launch branch too.
printf '#!/usr/bin/env bash\necho "%s/nowhere/$*"\n' "$BOX" > "$REPO/scripts/sutando-config.sh"
touch "$REPO/scripts/connections.py" "$REPO/src/remote-gateway-bridge.py"

# The stand-in interpreter: answers by the script it is handed and logs each call.
cat > "$BOX/py" <<'EOF'
#!/usr/bin/env bash
script="$1"; shift
case "$script" in
  */scripts/connections.py)
    echo "connections $*" >> "$FAKE_OUT/calls"
    case "$1" in
      import-env) [ "${FAKE_IMPORT_FAIL:-0}" = 1 ] && { echo "vault down" >&2; exit 1; }; exit 0;;
      launch-plan) [ "${FAKE_PLAN_FAIL:-0}" = 1 ] && { echo "connections: connections.json: not valid JSON" >&2; exit 1; }
                   cat "$FAKE_PLAN";;
      launch) [ "$2" = notoken ] && { echo "connections: no token under AG2_REMOTE_TOKEN_NOTOKEN" >&2; exit 1; }
              printf 'REMOTE_TASK_TOKEN=%s\n' "${REMOTE_TASK_TOKEN-<unset>}" > "$FAKE_OUT/$2.env";;
    esac;;
  */remote-gateway-bridge.py) printf 'GATEWAY_INSTANCE=%s\n' "${GATEWAY_INSTANCE-<unset>}" > "$FAKE_OUT/primary.env";;
esac
EOF
chmod +x "$BOX/py" "$REPO/scripts/sutando-config.sh"

printf 'dev\tAG2_REMOTE_TOKEN_DEV\tdev-ag2space\t0\ns-space\tAG2_REMOTE_TOKEN_S_SPACE\tcustom-dir\t1\nnotoken\tAG2_REMOTE_TOKEN_NOTOKEN\tnotoken-ag2space\t0\n' > "$BOX/plan"

run_lanes() {
  env -i PATH="$PATH" HOME="$BOX" REPO="$REPO" PY="$BOX/py" LOGS_DIR="$LOGS_DIR" \
      FAKE_PLAN="$BOX/plan" FAKE_OUT="$OUT" \
      REMOTE_TASK_TOKEN="https://gw.primary.example|primary-secret" \
      AG2_REMOTE_TOKEN_DEV="https://gw.dev.example|env-secret" "$@" \
      bash -c '. "$REPO/src/startup-runtime.sh" >/dev/null 2>&1; start_gateway_lanes; wait' 2>&1
}

echo "== import-env runs first, then one launch per plan line =="
out="$(run_lanes)"
sleep 1
calls="$(cat "$OUT/calls" 2>/dev/null)"
case "$calls" in "connections import-env"*) ok "import-env is the first connections call";; *) bad "calls: $calls";; esac
grep -q "^connections launch-plan$" "$OUT/calls" && ok "launch-plan asked once" || bad "no launch-plan call"
[ "$(grep -c "^connections launch " "$OUT/calls")" = 3 ] && ok "one launch per plan line" || bad "launch calls: $(grep -c '^connections launch ' "$OUT/calls")"
grep -q "^connections launch dev --repo $REPO --logs-dir $LOGS_DIR$" "$OUT/calls" && ok "launch gets the instance, repo and logs dir" \
  || bad "dev launch line: $(grep 'launch dev' "$OUT/calls")"
grep -q "^connections launch s-space " "$OUT/calls" && ok "a primary plan row is launched too" || bad "s-space not launched"
[ "$(sed -n 's/^REMOTE_TASK_TOKEN=//p' "$OUT/dev.env")" = "https://gw.primary.example|primary-secret" ] \
  && ok "the shell passes its environment through and composes no bridge env of its own" \
  || bad "dev env: $(cat "$OUT/dev.env" 2>/dev/null)"
case "$out" in *"gateway bridge (dev — self-defers"*) ok "a launch is reported";; *) bad "no dev line: $out";; esac
case "$out" in *"gateway bridge (notoken) not launched"*) ok "a refused launch is reported, not hidden";; *) bad "no notoken line: $out";; esac
case "$out" in *"primary-secret"*|*"env-secret"*|*"AG2_REMOTE_TOKEN_NOTOKEN"*) bad "a secret or key name reached stdout";; *) ok "no secret on stdout";; esac
grep -q "no token under AG2_REMOTE_TOKEN_NOTOKEN" "$LOGS_DIR/remote-gateway-bridge.log" && ok "the refusal is in the bridge log" || bad "refusal not logged"
[ -f "$OUT/primary.env" ] && [ "$(sed -n 's/^GATEWAY_INSTANCE=//p' "$OUT/primary.env")" = "<unset>" ] \
  && ok "the primary launch path is untouched" || bad "primary launch changed: $(cat "$OUT/primary.env" 2>/dev/null)"

echo "== a failed import-env still launches from the plan, and says so =="
rm -f "$OUT"/*.env "$OUT/calls"
out="$(run_lanes FAKE_IMPORT_FAIL=1)"
sleep 1
case "$out" in *"import-env failed"*) ok "import failure is reported";; *) bad "no import-failure line: $out";; esac
[ -f "$OUT/dev.env" ] && ok "dev bridge still launched" || bad "dev bridge missing after import failure"

echo "== an unreadable record is said out loud and the environment's tokens still launch =="
rm -f "$OUT"/*.env "$OUT/calls"
out="$(run_lanes FAKE_PLAN_FAIL=1)"
sleep 1
case "$out" in *"⚠ connections launch-plan failed"*) ok "the plan failure is reported, not read as an empty plan";; *) bad "no plan-failure line: $out";; esac
grep -q "^connections launch dev .* --env-only$" "$OUT/calls" && ok "the environment's dev token launches with --env-only" \
  || bad "no env-only launch: $(cat "$OUT/calls")"
[ "$(grep -c '^connections launch ' "$OUT/calls")" = 1 ] && ok "only the environment's instances launch in the fallback" \
  || bad "fallback launched: $(grep '^connections launch ' "$OUT/calls")"

echo "== the old per-env-var launch loop is gone =="
envloop="$(awk '/for _gw_var in /{f=1} f{print} f&&/^[[:space:]]*done[[:space:]]*$/{exit}' "$REPO_UNDER_TEST/src/startup-runtime.sh")"
printf '%s' "$envloop" | grep -q 'remote-gateway-bridge.py' \
  && bad "startup-runtime.sh still launches bridges from env vars itself" || ok "the env-var loop only names skipped instances"
grep -q 'GATEWAY_INSTANCE="\$_gw_inst"' "$REPO_UNDER_TEST/src/startup-runtime.sh" \
  && bad "startup-runtime.sh still composes the bridge env" || ok "the bridge env shape lives in src/connections.py"

echo
echo "passed $pass, failed $fail"
[ "$fail" -eq 0 ]

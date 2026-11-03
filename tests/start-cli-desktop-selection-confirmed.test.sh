#!/usr/bin/env bash
# A desktop --runtime switch writes runtime.json only once the new core is confirmed. Real
# dispatcher, config CLI and record helper; launchers and tmux are stubs.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
PY_BIN="$(command -v python3)"
fails=0
say() { echo "$1  $2"; [ "$1" = FAIL ] && fails=$((fails+1)); return 0; }

APP="$TD/app"; FIX="$APP/engine/sutando"
mkdir -p "$APP/run" "$FIX/src/agent/claude/cli" "$FIX/src/agent/codex/cli" "$FIX/scripts" "$FIX/bin"
for f in src/agent/start-cli.sh src/agent/restart-guard.sh src/agent/tmux-socket-guard.sh \
         src/agent/core-runtime-record.sh src/sutando_config.py \
         scripts/sutando-config.sh scripts/python-binary.sh; do
  cp "$REPO/$f" "$FIX/$f"
done
echo '{}' > "$FIX/sutando.config.json"
for rt in claude codex; do
  cat > "$FIX/src/agent/$rt/cli/start-cli.sh" <<EOF
#!/bin/bash
REPO="$FIX"; TMUX_SOCKET="\$SUTANDO_TMUX_SOCKET"; SESSION=sutando-core
. "\$REPO/src/agent/core-runtime-record.sh"
echo "LAUNCHER $rt \$*" >> "\$TMUX_LOG"
case "\$LAUNCH_RESULT" in
  ok) record_confirmed_core_runtime $rt "\$SUTANDO_PY"; exit 0 ;;
  *) exit "\$LAUNCH_RESULT" ;;
esac
EOF
done
cat > "$FIX/bin/tmux" <<'EOF'
#!/bin/bash
echo "$*" >> "$TMUX_LOG"
[ "${1:-}" = -V ] && { echo "tmux 3.5a"; exit 0; }
case "$*" in
  *list-sessions*) echo "3.5a" ;;
  *show-environment*SUTANDO_CORE_RUNTIME*) echo "SUTANDO_CORE_RUNTIME=$ACTIVE" ;;
esac
exit 0
EOF
chmod +x "$FIX/bin/tmux" "$FIX"/src/agent/*/cli/start-cli.sh

# switch <active runtime> <requested runtime> <launcher result: ok|127|1>
switch() {
  : > "$TD/log"
  env -i PATH="$FIX/bin:/usr/bin:/bin" HOME="$TD" TMUX_LOG="$TD/log" SUTANDO_PY="$PY_BIN" \
    SUTANDO_WORKSPACE="$TD/ws" ACTIVE="$1" LAUNCH_RESULT="$3" \
    /bin/bash "$FIX/src/agent/start-cli.sh" --runtime "$2" > "$TD/out" 2> "$TD/err" < /dev/null
  rc=$?
}
seed() { printf '{"coreRuntime": "%s", "corePath": "/x/sutando"}\n' "$1" > "$APP/runtime.json"; }

for direction in "claude codex" "codex claude"; do
  set -- $direction; from="$1"; to="$2"
  for code in 127 1; do
    seed "$from"; cp "$APP/runtime.json" "$TD/before"
    switch "$from" "$to" "$code"
    if [ "$rc" = "$code" ] && grep -q "LAUNCHER $to --restart" "$TD/log" \
       && cmp -s "$TD/before" "$APP/runtime.json"; then
      say PASS "$from -> $to, launcher exits $code: runtime.json still selects $from"
    else
      say FAIL "$from -> $to exit $code: rc=$rc runtime.json=$(cat "$APP/runtime.json") err=$(tr '\n' '|' < "$TD/err")"
    fi
  done
  seed "$from"
  switch "$from" "$to" ok
  got="$("$PY_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["coreRuntime"], d["corePath"])' "$APP/runtime.json")"
  if [ "$rc" = 0 ] && [ "$got" = "$to /x/sutando" ] && grep -q "desktop runtime selection is now $to" "$TD/err"; then
    say PASS "$from -> $to, confirmed core: runtime.json selects $to and keeps its other keys"
  else
    say FAIL "$from -> $to confirmed: rc=$rc got=$got err=$(tr '\n' '|' < "$TD/err")"
  fi
done

printf '{' > "$APP/runtime.json"
switch claude codex ok
if [ "$rc" = 1 ] && ! grep -q LAUNCHER "$TD/log" && [ "$(cat "$APP/runtime.json")" = "{" ]; then
  say PASS "an unwritable selection refuses before the launcher and is left as it was"
else
  say FAIL "unwritable selection: rc=$rc log=$(tr '\n' '|' < "$TD/log") err=$(tr '\n' '|' < "$TD/err")"
fi

[ "$fails" = 0 ] && echo "all confirmed desktop selection checks pass" || { echo "$fails failed"; exit 1; }

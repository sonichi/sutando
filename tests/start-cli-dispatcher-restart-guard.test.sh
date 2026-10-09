#!/usr/bin/env bash
# The dispatcher refuses an in-session restart before reaping any helper, and audits the refusal.
# tmux, sutando-config and the runtime launcher are stubs; nothing real is touched.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
fails=0
say() { echo "$1  $2"; [ "$1" = FAIL ] && fails=$((fails+1)); return 0; }

FIX="$TD/repo"; mkdir -p "$FIX/src/agent/claude/cli" "$FIX/src/agent/codex/cli" "$FIX/scripts" "$FIX/bin"
cp "$REPO/src/agent/start-cli.sh" "$REPO/src/agent/restart-guard.sh" "$FIX/src/agent/"
for rt in claude codex; do
  printf '#!/bin/bash\necho "LAUNCHER %s $*" >> "$TMUX_LOG"\n' "$rt" > "$FIX/src/agent/$rt/cli/start-cli.sh"
done
printf '#!/bin/bash\n[ "$1" = workspace ] && echo "$TEST_WS"\n' > "$FIX/scripts/sutando-config.sh"
cat > "$FIX/bin/tmux" <<'EOF'
#!/bin/bash
echo "$*" >> "$TMUX_LOG"
case "$*" in
  *has-session*) exit 0 ;;
  *show-environment*SUTANDO_CORE_RUNTIME*) echo "SUTANDO_CORE_RUNTIME=$LIVE_RUNTIME" ;;
esac
exit 0
EOF
chmod +x "$FIX/bin/tmux" "$FIX/scripts/sutando-config.sh" "$FIX"/src/agent/*/cli/start-cli.sh
AUDIT="$TD/ws/logs/restart-attempts.log"

# run <live runtime> <requested runtime> <flag or ""> [env...]
run() {
  local live="$1" want="$2" flag="$3"; shift 3
  : > "$TD/log"; rm -f "$AUDIT"
  env -i PATH="$FIX/bin:/usr/bin:/bin" HOME="$TD" TMUX_LOG="$TD/log" TEST_WS="$TD/ws" LIVE_RUNTIME="$live" \
    SUTANDO_TMUX_SOCKET="$TD/sock" "$@" \
    /bin/bash "$FIX/src/agent/start-cli.sh" --runtime "$want" ${flag:+"$flag"} > "$TD/out" 2> "$TD/err" < /dev/null
  rc=$?
}
refused() {  # $1 label, $2 audit kind
  if [ "$rc" != 0 ] && ! grep -q kill-session "$TD/log" && ! grep -q LAUNCHER "$TD/log" \
     && grep -q "refusing --restart from inside the sutando-core session" "$TD/err" \
     && [ "$(grep -c "\[$2\] refused: inherited SUTANDO_CORE_SESSION=1" "$AUDIT" 2>/dev/null)" = 1 ]; then
    say PASS "$1: refused and audited, no watcher/observer/core kill, no launcher"
  else
    say FAIL "$1: rc=$rc log=$(tr '\n' '|' < "$TD/log") audit=$(cat "$AUDIT" 2>/dev/null)"
  fi
}

run codex claude "" SUTANDO_CORE_SESSION=1; refused "synthesized Codex -> Claude restart" restart
run claude claude --restart SUTANDO_CORE_SESSION=1; refused "explicit --restart" restart
run claude claude --force-restart SUTANDO_CORE_SESSION=1; refused "explicit --force-restart" force-restart

run codex claude ""
if [ "$rc" = 0 ] && grep -q "kill-session -t =sutando-core-watcher" "$TD/log" \
   && grep -q "kill-session -t =sutando-core-observer" "$TD/log" && grep -q "LAUNCHER claude --restart" "$TD/log" \
   && [ ! -e "$AUDIT" ]; then
  say PASS "unmarked caller: helpers reaped, restart delegated, nothing refused"
else
  say FAIL "unmarked caller: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

run codex claude "" SUTANDO_CORE_SESSION=1 SUTANDO_ALLOW_INSESSION_RESTART=1
if [ "$rc" = 0 ] && grep -q "kill-session -t =sutando-core-observer" "$TD/log" && grep -q "LAUNCHER claude --restart" "$TD/log"; then
  say PASS "explicit override: helpers reaped, restart delegated"
else
  say FAIL "explicit override: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

[ "$fails" = 0 ] && echo "all dispatcher restart-guard checks pass" || { echo "$fails failed"; exit 1; }

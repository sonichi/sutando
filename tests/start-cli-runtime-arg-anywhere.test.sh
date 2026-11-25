#!/usr/bin/env bash
# `--runtime X` selects the runtime wherever it appears in the dispatcher's args.
# graceful-restart.sh execs `start-cli.sh --restart <args>`, so a relayed
# `--runtime claude` lands AFTER --restart; a leading-only parse dropped it and
# relaunched the configured runtime. tmux, sutando-config and both launchers
# are stubs; nothing real is touched.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
fails=0
say() { echo "$1  $2"; [ "$1" = FAIL ] && fails=$((fails+1)); return 0; }

FIX="$TD/repo"; mkdir -p "$FIX/src/agent/claude/cli" "$FIX/src/agent/codex/cli" "$FIX/scripts" "$FIX/bin"
cp "$REPO/src/agent/start-cli.sh" "$REPO/src/agent/restart-guard.sh" "$REPO/src/agent/tmux-socket-guard.sh" \
   "$REPO/src/agent/launch-path.sh" "$REPO/src/agent/graceful-restart.sh" "$FIX/src/agent/"
for rt in claude codex; do
  printf '#!/bin/bash\necho "LAUNCHER %s $*" >> "$TMUX_LOG"\n' "$rt" > "$FIX/src/agent/$rt/cli/start-cli.sh"
done
# The configured runtime is always codex: a dropped --runtime shows as LAUNCHER codex.
cat > "$FIX/scripts/sutando-config.sh" <<'EOF2'
#!/bin/bash
case "$1" in
  workspace) echo "$TEST_WS" ;;
  core-runtime) echo codex ;;
  host-label) echo testhost ;;
esac
EOF2
cat > "$FIX/bin/tmux" <<'EOF2'
#!/bin/bash
echo "$*" >> "$TMUX_LOG"
case "$*" in
  *has-session*) exit 0 ;;
  *show-environment*SUTANDO_CORE_RUNTIME*) echo "SUTANDO_CORE_RUNTIME=$LIVE_RUNTIME" ;;
esac
exit 0
EOF2
chmod +x "$FIX/bin/tmux" "$FIX/scripts/sutando-config.sh" "$FIX"/src/agent/*/cli/start-cli.sh

run() {  # run <live runtime> <dispatcher args...>
  local live="$1"; shift
  : > "$TD/log"
  env -i PATH="$FIX/bin:/usr/bin:/bin" HOME="$TD" TMUX_LOG="$TD/log" TEST_WS="$TD/ws" LIVE_RUNTIME="$live" \
    SUTANDO_TMUX_SOCKET="$TD/sock" \
    /bin/bash "$FIX/src/agent/start-cli.sh" "$@" > "$TD/out" 2> "$TD/err" < /dev/null
  rc=$?
}
launched() {  # launched <label> <expected LAUNCHER line>
  local got; got="$(grep '^LAUNCHER' "$TD/log" || true)"
  if [ "$rc" = 0 ] && [ "$got" = "$2" ]; then say PASS "$1"; else say FAIL "$1: rc=$rc got='$got' err=$(tr '\n' '|' < "$TD/err")"; fi
}

run codex --runtime claude --restart;              launched "leading --runtime (unchanged)" "LAUNCHER claude --restart"
run codex --restart --runtime claude;              launched "--runtime after --restart" "LAUNCHER claude --restart"
run codex --restart --visible --runtime claude;    launched "--runtime last, other args kept in order" "LAUNCHER claude --restart --visible"
run codex --restart --runtime=claude --visible;    launched "--runtime=claude form" "LAUNCHER claude --restart --visible"
run codex --visible --runtime claude;              launched "switch without --restart synthesizes one" "LAUNCHER claude --restart --visible"
run codex --restart;                               launched "no --runtime falls back to config" "LAUNCHER codex --restart"

run codex --restart --runtime
if [ "$rc" = 2 ] && grep -q -- "--runtime needs a value" "$TD/err" && ! grep -q LAUNCHER "$TD/log"; then
  say PASS "trailing --runtime without a value is refused"
else say FAIL "trailing --runtime: rc=$rc"; fi
run codex --restart --runtime=
if [ "$rc" = 2 ] && ! grep -q LAUNCHER "$TD/log"; then say PASS "empty --runtime= refused"; else say FAIL "empty --runtime=: rc=$rc"; fi
run codex --restart --runtime gemini
if [ "$rc" = 2 ] && grep -q "unsupported core runtime: gemini" "$TD/err"; then say PASS "unknown runtime refused"; else say FAIL "unknown runtime: rc=$rc"; fi

# End to end through graceful-restart: dead core (no .alive) => no wait; it execs
# `start-cli.sh --restart --runtime claude` and the dispatcher must honor it.
mkdir -p "$TD/ws/state"
: > "$TD/log"
env -i PATH="$FIX/bin:/usr/bin:/bin" HOME="$TD" TMUX_LOG="$TD/log" TEST_WS="$TD/ws" LIVE_RUNTIME=codex \
  SUTANDO_TMUX_SOCKET="$TD/sock" GR_WS="$TD/ws" GR_POLL_S=0 \
  /bin/bash "$FIX/src/agent/graceful-restart.sh" -- --runtime claude > "$TD/out" 2> "$TD/err" < /dev/null
rc=$?
launched "graceful-restart relays --runtime past --restart" "LAUNCHER claude --restart"

[ "$fails" = 0 ] && echo "all --runtime placement checks pass" || { echo "$fails failed"; exit 1; }

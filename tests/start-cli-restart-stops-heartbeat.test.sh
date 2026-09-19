#!/usr/bin/env bash
# --restart hands the heartbeat over: after the old pane is killed and before the fresh core is
# created, the launcher runs `core_heartbeat.py --stop` and logs what it stopped. A plain launch
# never does. Stubs tmux/pgrep/ps/claude and the interpreter — no real core or writer is touched.
# Run: bash tests/start-cli-restart-stops-heartbeat.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/src/agent/claude/cli/start-cli.sh"
FAKEPID=999999
fails=0
say() { echo "$1  $2"; if [ "$1" = "FAIL" ]; then fails=$((fails+1)); fi; return 0; }

TD="$(mktemp -d)"
trap 'rm -rf "$TD"' EXIT
BIN="$TD/bin"; mkdir -p "$BIN"
export SESS_MARK="$TD/sess" CORE_MARK="$TD/core" PY_ARGV="$TD/py-argv"
REAL_PY="$(command -v python3)"

cat > "$BIN/tmux" <<'STUB'
#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
case "$sub" in
  has-session)  [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session)  touch "$SESS_MARK"; exit 0 ;;
  kill-session) rm -f "$SESS_MARK" "$CORE_MARK"; exit 0 ;;
  *) exit 0 ;;
esac
STUB
cat > "$BIN/pgrep" <<STUB
#!/bin/bash
case "\$*" in *core-input-watch*) exit 0 ;; esac
case "\$*" in
  *claude*) if [ -f "\$CORE_MARK" ]; then echo "$FAKEPID claude --name sutando-core"; exit 0; else exit 1; fi ;;
esac
exit 1
STUB
cat > "$BIN/ps" <<STUB
#!/bin/bash
want=""; prev=""
for a in "\$@"; do [ "\$prev" = "-p" ] && want="\$a"; prev="\$a"; done
[ "\$want" = "$FAKEPID" ] && [ -f "\$CORE_MARK" ] && echo "claude --name sutando-core"
exit 0
STUB
# The interpreter stand-in: a core_heartbeat.py invocation is recorded (argv + whether the old
# session still existed) and answered like a real --stop; anything else runs on the real python.
cat > "$BIN/python3" <<STUB
#!/bin/bash
case "\$*" in *core_heartbeat.py*)
  { printf 'argv: %s\n' "\$*"; [ -f "\$SESS_MARK" ] && echo "session: present" || echo "session: absent"; } >> "\$PY_ARGV"
  [ "\${STOP_RC:-0}" = 0 ] || exit "\$STOP_RC"
  echo "core_heartbeat: stopped 1 writer(s): 4242"; exit 0 ;;
esac
exec "$REAL_PY" "\$@"
STUB
printf '#!/bin/bash\nexit 0\n' > "$BIN/claude"
chmod +x "$BIN"/*

# TEST_MODE+WORKSPACE keep writes in $TD; SUTANDO_PY pins the stand-in; ANTHROPIC_BASE_URL skips a bind wait.
run_launcher() {
  local flag="$1"; shift
  : > "$SESS_MARK"; : > "$CORE_MARK"; rm -f "$PY_ARGV"
  env -i PATH="$BIN:/usr/bin:/bin" HOME="$TD" \
      SESS_MARK="$SESS_MARK" CORE_MARK="$CORE_MARK" PY_ARGV="$PY_ARGV" STOP_RC="${STOP_RC:-0}" \
      SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$TD/workspace" SUTANDO_PY="$BIN/python3" \
      SUTANDO_TMUX_SOCKET="$TD/sock" \
      ANTHROPIC_BASE_URL="http://localhost:7846" "$@" \
      /bin/bash "$SCRIPT" ${flag:+"$flag"} > "$TD/stdout" 2> "$TD/stderr" < /dev/null
  rc=$?
  out="$(cat "$TD/stdout")"; err="$(cat "$TD/stderr")"; both="$out$err"
  argv="$(cat "$PY_ARGV" 2>/dev/null || true)"
}
LOG="$TD/workspace/logs/restart-attempts.log"

# --- --restart: the recorded writer is stopped between the kill and the fresh core ---------
run_launcher --restart
case "$both" in *"Killing existing"*) say ok "--restart reaches the kill path" ;;
  *) say FAIL "restart branch not entered: $(printf '%s' "$both" | tail -3)" ;; esac
case "$argv" in *"argv: $REPO/src/core_heartbeat.py --stop"*) say ok "--restart invokes core_heartbeat.py --stop with this checkout's script" ;;
  *) say FAIL "no --stop invocation recorded: [$argv]" ;; esac
case "$argv" in *"session: absent"*) say ok "--stop runs after the old session is gone" ;;
  *) say FAIL "--stop ran while the old session still existed (or never ran)" ;; esac
[ "$(grep -c 'argv: ' "$PY_ARGV" 2>/dev/null)" = "1" ] && say ok "exactly one heartbeat invocation" \
  || say FAIL "expected one heartbeat invocation, got: $(grep -c 'argv: ' "$PY_ARGV" 2>/dev/null)"
case "$argv" in *" -9"*|*"SIGKILL"*|*"kill "*) say FAIL "the handoff must go through --stop, never a kill" ;;
  *) say ok "no direct kill of the writer" ;; esac
grep -q 'heartbeat handoff: core_heartbeat: stopped 1 writer(s): 4242' "$LOG" 2>/dev/null \
  && say ok "restart-attempts.log records what was stopped" \
  || say FAIL "restart-attempts.log lacks the handoff line: $(cat "$LOG" 2>/dev/null | tail -3)"
# ordering in the log: begin < handoff < kill-complete
if [ -r "$LOG" ]; then
  b="$(grep -n 'begin (session=' "$LOG" | tail -1 | cut -d: -f1)"
  h="$(grep -n 'heartbeat handoff:' "$LOG" | tail -1 | cut -d: -f1)"
  k="$(grep -n 'kill-complete; creating fresh core' "$LOG" | tail -1 | cut -d: -f1)"
  [ -n "$b" ] && [ -n "$h" ] && [ -n "$k" ] && [ "$b" -lt "$h" ] && [ "$h" -lt "$k" ] \
    && say ok "log order: begin < handoff < kill-complete" \
    || say FAIL "log order wrong (begin=$b handoff=$h kill-complete=$k)"
fi

# --- --force-restart takes the same handoff ------------------------------------------------
run_launcher --force-restart
case "$argv" in *"argv: $REPO/src/core_heartbeat.py --stop"*) say ok "--force-restart hands the heartbeat over too" ;;
  *) say FAIL "--force-restart skipped the handoff" ;; esac

# --- plain launch: unchanged, never stops a writer -----------------------------------------
run_launcher ""
case "$argv" in *"--stop"*) say FAIL "a plain launch stopped the writer" ;;
  *) say ok "plain launch never invokes --stop" ;; esac
case "$both" in *"Killing existing"*) say FAIL "plain launch entered the kill path" ;;
  *) say ok "plain launch does not kill" ;; esac

# --- --stop fails: said aloud and logged, the restart still proceeds -----------------------
# (No "no interpreter" case: the launcher refuses to start without a python long before this block.)
STOP_RC=1 run_launcher --restart
case "$err" in *"WARN heartbeat handoff (--stop) failed"*) say ok "a failed --stop is said aloud on stderr" ;;
  *) say FAIL "a failed --stop was silent: $(printf '%s' "$err" | tail -3)" ;; esac
grep -q 'heartbeat handoff FAILED' "$LOG" 2>/dev/null && say ok "a failed --stop is logged" \
  || say FAIL "a failed --stop is not in restart-attempts.log: $(tail -3 "$LOG" 2>/dev/null)"
case "$both" in *"Killing existing"*) say ok "a failed --stop does not abort the restart" ;;
  *) say FAIL "restart aborted on a failed --stop" ;; esac

# --- hygiene: the stub runs stayed inside the sandbox ---------------------------------------
live_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
if [ -n "$live_ws" ] && [ "$live_ws" != "$TD/workspace" ] && [ -r "$live_ws/logs/restart-attempts.log" ]; then
  grep -q 'stopped 1 writer(s): 4242' "$live_ws/logs/restart-attempts.log" \
    && say FAIL "a stub run appended to the live workspace log" \
    || say ok "no stub run appended to the live workspace log"
fi

[ "$fails" -eq 0 ] && echo "PASS  --restart hands the heartbeat over between kill and fresh core; plain launch unchanged." \
  || echo "FAIL  $fails assertion(s)"
exit $(( fails > 0 ? 1 : 0 ))

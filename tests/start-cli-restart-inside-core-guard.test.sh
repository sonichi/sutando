#!/usr/bin/env bash
# Asserts the in-session --restart guard in src/agent/claude/cli/start-cli.sh,
# both directions: refuses on an inherited SUTANDO_CORE_SESSION=1, else proceeds.

# Stubs tmux/pgrep/ps/claude on PATH — no real tmux, core or session is touched.
# Run: bash tests/start-cli-restart-inside-core-guard.test.sh
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO/src/agent/claude/cli/start-cli.sh"
FAKEPID=999999
fails=0
say() { echo "$1  $2"; if [ "$1" = "FAIL" ]; then fails=$((fails+1)); fi; return 0; }

TD="$(mktemp -d)"
trap 'rm -rf "$TD"' EXIT
BIN="$TD/bin"; mkdir -p "$BIN"
export SESS_MARK="$TD/sess" CORE_MARK="$TD/core"

cat > "$BIN/tmux" <<'EOF'
#!/bin/bash
while [ "$1" = "-S" ]; do shift 2; done
sub="$1"; shift
case "$sub" in
  has-session)  [ -f "$SESS_MARK" ] && exit 0 || exit 1 ;;
  new-session)  touch "$SESS_MARK"; exit 0 ;;
  kill-session) rm -f "$SESS_MARK" "$CORE_MARK"; exit 0 ;;
  *) exit 0 ;;
esac
EOF
cat > "$BIN/pgrep" <<EOF
#!/bin/bash
case "\$*" in *core-input-watch*) exit 0 ;; esac
case "\$*" in
  *claude*) if [ -f "\$CORE_MARK" ]; then echo "$FAKEPID claude --name sutando-core"; exit 0; else exit 1; fi ;;
esac
exit 1
EOF
cat > "$BIN/ps" <<EOF
#!/bin/bash
want=""; prev=""
for a in "\$@"; do [ "\$prev" = "-p" ] && want="\$a"; prev="\$a"; done
[ "\$want" = "$FAKEPID" ] && [ -f "\$CORE_MARK" ] && echo "claude --name sutando-core"
exit 0
EOF
printf '#!/bin/bash\nexit 0\n' > "$BIN/claude"
chmod +x "$BIN"/*

# $1 = flag; rest = extra env. Sets rc/out/err/both, always from "session + core
# alive" so the non-refusing path has something to kill.

# TEST_MODE+WORKSPACE keep writes in $TD; ANTHROPIC_BASE_URL skips a ~10s bind wait.
run_launcher() {
  local flag="$1"; shift
  : > "$SESS_MARK"; : > "$CORE_MARK"
  env -i PATH="$BIN:/usr/bin:/bin" HOME="$TD" \
      SESS_MARK="$SESS_MARK" CORE_MARK="$CORE_MARK" \
      SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$TD/workspace" \
      SUTANDO_TMUX_SOCKET="$TD/sock" \
      ANTHROPIC_BASE_URL="http://localhost:7846" "$@" \
      /bin/bash "$SCRIPT" ${flag:+"$flag"} > "$TD/stdout" 2> "$TD/stderr" < /dev/null
  rc=$?
  out="$(cat "$TD/stdout")"; err="$(cat "$TD/stderr")"; both="$out$err"
}

# Refusals must land in the same log every other restart attempt is diagnosed
# from — here the sandboxed copy under $TD, so no live workspace is touched.
LOG="$TD/workspace/logs/restart-attempts.log"
count_refusals() {
  if [ -r "$LOG" ]; then
    grep -c "refused: inherited SUTANDO_CORE_SESSION=1" "$LOG" 2>/dev/null
  else
    echo 0
  fi
}
log_pre="$(count_refusals)"
live_log_lines() {
  local f; f="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
  [ -n "$f" ] && [ -r "$f/logs/restart-attempts.log" ] \
    && wc -l < "$f/logs/restart-attempts.log" || echo 0
}
live_log_pre="$(live_log_lines)"

REFUSAL="refusing --restart from inside the sutando-core session"

# --- direction 1: inherited marker -> refuse ------------------------------
run_launcher --restart SUTANDO_CORE_SESSION=1
[ "$rc" -ne 0 ] && say ok "in-session --restart exits non-zero (rc=$rc)" \
  || say FAIL "in-session --restart exited 0 — the self-kill was allowed"
case "$err" in *"$REFUSAL"*) say ok "refusal reaches the caller on stderr" ;;
  *) say FAIL "no refusal on stderr: $(printf '%s' "$both" | tail -3)" ;; esac
case "$out" in *"$REFUSAL"*) say FAIL "refusal was written to stdout" ;;
  *) say ok "refusal is stderr-only" ;; esac
case "$both" in *"Killing existing"*)
    say FAIL "guard did not short-circuit — the kill path still ran" ;;
  *) say ok "never reaches kill-session" ;; esac
# the guard must TELL the caller what to do instead
for want in "restart core" "Sutando.app" "launchd" "terminal OUTSIDE the core" \
            "NOT --emit-task" "SUTANDO_ALLOW_INSESSION_RESTART=1"; do
  case "$err" in *"$want"*) say ok "message names: $want" ;;
    *) say FAIL "message omits: $want" ;; esac
done

# --- direction 2: no inherited marker -> do NOT refuse --------------------
run_launcher --restart
case "$both" in *"$REFUSAL"*)
    say FAIL "refused a legitimate out-of-session --restart" ;;
  *) say ok "out-of-session --restart is not refused" ;; esac
case "$both" in *"Killing existing"*) say ok "out-of-session --restart reaches the kill path" ;;
  *) say FAIL "restart branch not entered: $(printf '%s' "$both" | tail -3)" ;; esac

# --- escape hatch: inherited marker + documented override -> proceed ------
run_launcher --restart SUTANDO_CORE_SESSION=1 SUTANDO_ALLOW_INSESSION_RESTART=1
case "$both" in *"$REFUSAL"*) say FAIL "override did not release the guard" ;;
  *) say ok "SUTANDO_ALLOW_INSESSION_RESTART=1 releases the guard" ;; esac
case "$both" in *"Killing existing"*) say ok "override reaches the kill path" ;;
  *) say FAIL "override did not reach the restart branch" ;; esac

# --- flag scope: --force-restart guarded too, no flag never guarded -------
run_launcher --force-restart SUTANDO_CORE_SESSION=1
case "$err" in *"$REFUSAL"*) say ok "--force-restart is guarded too" ;;
  *) say FAIL "--force-restart escaped the guard" ;; esac
run_launcher "" SUTANDO_CORE_SESSION=1
case "$both" in *"$REFUSAL"*) say FAIL "a plain in-core launch was refused" ;;
  *) say ok "no restart flag in-core -> not refused" ;; esac

# --- the refusal is recorded where restart attempts are diagnosed ---------
[ "$(count_refusals)" -gt "$log_pre" ] \
  && say ok "refusal appended to restart-attempts.log" \
  || say FAIL "refusal never reached restart-attempts.log (was $log_pre)"

# --- hygiene: the stub runs stayed inside the sandbox ---------------------
live_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
if [ -n "$live_ws" ] && [ "$live_ws" != "$TD/workspace" ]; then
  [ "$live_log_pre" = "$(live_log_lines)" ] \
    && say ok "no stub run appended to the live workspace log" \
    || say FAIL "a stub run appended to $live_ws/logs/restart-attempts.log"
fi

[ "$fails" -eq 0 ] && echo "PASS  in-session --restart is refused; out-of-session is not." \
  || echo "FAIL  $fails assertion(s)"
exit $(( fails > 0 ? 1 : 0 ))

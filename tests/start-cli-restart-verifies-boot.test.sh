#!/usr/bin/env bash
# A --restart must report success only once the NEW core boots (crons stamp or
# session transcript written after launch), never on pane presence alone.

# Stubs tmux/pgrep/ps/claude on PATH — the stub core is "present" the moment
# new-session runs and never boots by itself; each case plants the evidence.

# Run: bash tests/start-cli-restart-verifies-boot.test.sh
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
  new-session)  touch "$SESS_MARK" "$CORE_MARK"; exit 0 ;;
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

WS="$TD/workspace"
HOST_LABEL="boot-test-host"
# Every run shares this env; the boot wait is bounded to 4s (120s in production).
BOOT_TIMEOUT=4
LOG="$WS/logs/restart-attempts.log"
STAMP="$WS/hosts/$HOST_LABEL/schedule-crons-stamp.json"

with_env() {
  env -i PATH="$BIN:/usr/bin:/bin" HOME="$TD" \
      SESS_MARK="$SESS_MARK" CORE_MARK="$CORE_MARK" \
      SUTANDO_TEST_MODE=1 SUTANDO_WORKSPACE="$WS" SUTANDO_HOST_LABEL="$HOST_LABEL" \
      SUTANDO_TMUX_SOCKET="$TD/sock" SUTANDO_RESTART_BOOT_TIMEOUT="$BOOT_TIMEOUT" \
      ANTHROPIC_BASE_URL="http://localhost:7846" "$@"
}

# The transcript dir the launcher will watch: the same CLAUDE_CONFIG_DIR it
# exports to the core, slugged the way Claude Code slugs the core's cwd.
CCD="$(with_env bash "$REPO/scripts/sutando-config.sh" claude-sutando-config-dir)"
SLUG="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from util_paths import claude_project_slug; print(claude_project_slug(sys.argv[2]), end="")' "$REPO/src" "$REPO")"
TDIR="$CCD/projects/$SLUG"

# $1 = flag ("" for a plain launch). Starts from "session + core alive" so a
# restart exercises the kill path, then the launch; no TTY, like Sutando.app.
run_launcher() {
  local flag="$1"
  : > "$SESS_MARK"; : > "$CORE_MARK"
  rm -f "$LOG"
  with_env /bin/bash "$SCRIPT" ${flag:+"$flag"} > "$TD/stdout" 2> "$TD/stderr" < /dev/null
  rc=$?
  out="$(cat "$TD/stdout")"; err="$(cat "$TD/stderr")"; both="$out$err"
  log="$(cat "$LOG" 2>/dev/null || true)"
}
# Plants a file after the launcher is already waiting (kill+launch take <2s).
plant_later() { ( sleep 2; mkdir -p "$(dirname "$1")"; echo '{"ts": 1}' > "$1" ) & }
reset_evidence() { rm -rf "$WS/hosts" "$TDIR"; }

live_log_lines() {
  local f; f="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
  [ -n "$f" ] && [ -r "$f/logs/restart-attempts.log" ] \
    && wc -l < "$f/logs/restart-attempts.log" || echo 0
}
live_log_pre="$(live_log_lines)"

# --- stamp written after launch -> booted ---------------------------------
reset_evidence
plant_later "$STAMP"
run_launcher --restart; wait
[ "$rc" -eq 0 ] && say ok "stamp after launch: exit 0" || say FAIL "stamp after launch: rc=$rc: $(printf '%s' "$both" | tail -3)"
case "$log" in *"success: core live (booted: stamp, "*"s)"*) say ok "log names the stamp as the boot evidence" ;;
  *) say FAIL "no 'booted: stamp' line: $log" ;; esac
case "$out" in *"Started sutando-core detached"*) say ok "the started message still prints after boot" ;;
  *) say FAIL "started message missing: $out" ;; esac

# --- transcript written after launch -> booted ----------------------------
reset_evidence
plant_later "$TDIR/11111111-2222-3333-4444-555555555555.jsonl"
run_launcher --restart; wait
[ "$rc" -eq 0 ] && say ok "transcript after launch: exit 0" || say FAIL "transcript after launch: rc=$rc: $(printf '%s' "$both" | tail -3)"
case "$log" in *"success: core live (booted: transcript, "*"s)"*) say ok "log names the transcript as the boot evidence" ;;
  *) say FAIL "no 'booted: transcript' line: $log" ;; esac

# --- neither -> started, not booted; non-zero -----------------------------
reset_evidence
run_launcher --restart
[ "$rc" -ne 0 ] && say ok "no evidence: exit non-zero (rc=$rc)" || say FAIL "no evidence: exited 0 — the false success is back"
case "$log" in *"started, not booted after ${BOOT_TIMEOUT}s"*) say ok "log reads 'started, not booted after ${BOOT_TIMEOUT}s'" ;;
  *) say FAIL "no 'started, not booted' line: $log" ;; esac
case "$log" in *"success: core live"*) say FAIL "a success line was logged without boot evidence" ;;
  *) say ok "no success line without boot evidence" ;; esac
case "$err" in *"did not boot within ${BOOT_TIMEOUT}s"*) say ok "failure reaches the caller on stderr" ;;
  *) say FAIL "no boot failure on stderr: $err" ;; esac
case "$out" in *"Started sutando-core detached"*) say FAIL "started message printed for an unbooted core" ;;
  *) say ok "no started message for an unbooted core" ;; esac

# --- a stale stamp (older than the restart) does not count ---------------
reset_evidence
mkdir -p "$(dirname "$STAMP")"; echo '{"ts": 1}' > "$STAMP"; touch -t 202001010000 "$STAMP"
run_launcher --restart
[ "$rc" -ne 0 ] && say ok "stale stamp: exit non-zero (rc=$rc)" || say FAIL "stale stamp: exited 0 — an old stamp counted as a boot"
case "$log" in *"started, not booted after"*) say ok "stale stamp: log reads 'started, not booted'" ;;
  *) say FAIL "stale stamp: no 'started, not booted' line: $log" ;; esac

# --- a stale transcript (older than the restart) does not count ----------
reset_evidence
mkdir -p "$TDIR"; : > "$TDIR/old.jsonl"; touch -t 202001010000 "$TDIR/old.jsonl"
run_launcher --restart
[ "$rc" -ne 0 ] && say ok "stale transcript: exit non-zero (rc=$rc)" || say FAIL "stale transcript: exited 0 — an old transcript counted as a boot"

# --- a plain launch keeps the presence check and never waits -------------
reset_evidence
t0=$(date +%s)
run_launcher ""
[ "$rc" -eq 0 ] && say ok "plain launch: exit 0 on presence alone" || say FAIL "plain launch: rc=$rc: $(printf '%s' "$both" | tail -3)"
[ $(( $(date +%s) - t0 )) -lt "$BOOT_TIMEOUT" ] && say ok "plain launch did not wait for boot" || say FAIL "plain launch waited the boot timeout"
case "$both" in *"waiting up to"*|*"booted"*) say FAIL "plain launch printed boot-wait text" ;;
  *) say ok "plain launch prints no boot-wait text" ;; esac

# --- hygiene: the stub runs stayed inside the sandbox ---------------------
live_ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null || true)"
if [ -n "$live_ws" ] && [ "$live_ws" != "$WS" ]; then
  [ "$live_log_pre" = "$(live_log_lines)" ] \
    && say ok "no stub run appended to the live workspace log" \
    || say FAIL "a stub run appended to $live_ws/logs/restart-attempts.log"
fi

if [ "$fails" -eq 0 ]; then echo "PASS  --restart reports success only on a booted core."; exit 0; fi
echo "FAIL  $fails assertion(s) failed"; exit 1

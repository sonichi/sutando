#!/usr/bin/env bash
# A standby that starts beside a ready session watcher (its duplicate check could
# not read the process table) must not take the session's sentinel; and a
# sentinel a standby already took is given back by the next session start.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-standby-sent.XXXXXX")"
mkdir -p "$WORK/stubbin" "$WORK/blindps"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
REAL_PS="$(command -v ps)"
# The host-wide snapshot fails; per-pid reads still answer, as under load.
cat > "$WORK/blindps/ps" <<EOF
#!/bin/bash
for a in "\$@"; do [ "\$a" = "-Ao" ] && exit 1; done
exec "$REAL_PS" "\$@"
EOF
chmod +x "$WORK/blindps/ps"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
start() {  # start <role> <name> [extra PATH prefix]; prints the pid
  local role="$1" name="$2" pre="${3:-}"
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$WS" PATH="${pre:+$pre:}$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$WS/tasks" --role "$role" --inbox "$WS/tasks" > "$WORK/$name.out" 2> "$WORK/$name.err" &
  echo $!
}
role_present() { local i v; for i in 1 2 3 4 5; do v="$(python3 "$REPO/src/watcher_identity.py" role-present session --inbox "$WS/tasks" --ready "$WS/state" 2>/dev/null)"; case "$v" in yes|no) printf '%s' "$v"; return 0 ;; esac; sleep 0.3; done; printf '%s' "${v:-unknown}"; }

echo "standby keeps the session watcher's sentinel:"
WS="$WORK/ws"; mkdir -p "$WS/tasks" "$WS/state"
s="$(start session session)"; PIDS+=("$s")
for i in $(seq 1 150); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$s" ] && break; sleep 0.1; done
SENT="$(ls "$WS"/state/*.pid 2>/dev/null | head -1)"
[ -n "$SENT" ] && [ "$(cat "$SENT")" = "$s" ] && [ "$(role_present)" = "yes" ]; check "(setup) the session watcher $s is ready" $? "$(tail -2 "$WORK/session.err" | tr '\n' '|')"

b="$(start standby standby "$WORK/blindps")"; PIDS+=("$b")
for i in $(seq 1 100); do grep -q 'standby not stamping\|starting without the duplicate check' "$WORK/standby.err" 2>/dev/null && break; sleep 0.1; done
sleep 1
alive "$b"; check "(setup) the standby started past its blind duplicate check" $? "$(tail -3 "$WORK/standby.err" | tr '\n' '|')"
[ "$(cat "$SENT" 2>/dev/null)" = "$s" ]; check "(a) the standby left the sentinel naming the session watcher" $? "sentinel names $(cat "$SENT" 2>/dev/null), standby is $b"
[ "$(role_present)" = "yes" ]; check "(a) ...so the session watcher still reads as ready" $?
grep -q "standby not stamping .*live watcher pid $s" "$WORK/standby.err"; check "(a) ...and the standby said why" $? "$(tail -3 "$WORK/standby.err" | tr '\n' '|')"

# (b) A sentinel a standby already holds (written by an unfixed standby) is
#     handed back by the next session start, which then yields as covered.
echo "$b" > "$SENT"
[ "$(role_present)" = "no" ]; check "(b setup) a sentinel naming the live standby leaves the session unready" $?
n="$(start session rearm)"; PIDS+=("$n")
for i in $(seq 1 100); do alive "$n" || break; sleep 0.1; done
grep -q '^WATCHER_HELD: ' "$WORK/rearm.out"; check "(b) the re-arm yields to the live session watcher" $? "$(tail -3 "$WORK/rearm.err" | tr '\n' '|')"
[ "$(cat "$SENT" 2>/dev/null)" = "$s" ]; check "(b) ...and re-stamps the sentinel with the session watcher's pid" $? "sentinel names $(cat "$SENT" 2>/dev/null); $(tail -3 "$WORK/rearm.err" | tr '\n' '|')"
[ "$(role_present)" = "yes" ]; check "(b) ...so the session watcher reads as ready again" $?
alive "$s" && alive "$b"; check "(b) ...with neither watcher signalled" $?

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

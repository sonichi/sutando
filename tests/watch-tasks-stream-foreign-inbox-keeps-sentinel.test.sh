#!/usr/bin/env bash
# A session watcher for an inbox outside its workspace (an inherited SUTANDO_WORKSPACE_DIR, as a test run
# on a live host has) must not take the workspace's sentinel from its ready watcher, nor remove it on exit.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-foreign-sent.XXXXXX")"
mkdir -p "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL -- "-$p" 2>/dev/null; [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
start() {  # start <inbox> <name>; the workspace is always $WS, as an inherited one would be; prints the pid
  local inbox="$1" name="$2"
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$WS" PATH="$WORK/stubbin:$PATH" \
      python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$WATCHER" "$inbox" --role session --inbox "$inbox" > "$WORK/$name.out" 2> "$WORK/$name.err" &
  echo $!
}
role_present() { local i v; for i in 1 2 3 4 5; do v="$(python3 "$REPO/src/watcher_identity.py" role-present session --inbox "$WS/tasks" --ready "$WS/state" 2>/dev/null)"; case "$v" in yes|no) printf '%s' "$v"; return 0 ;; esac; sleep 0.3; done; printf '%s' "${v:-unknown}"; }

echo "a foreign-inbox watcher keeps the workspace watcher's sentinel:"
WS="$WORK/ws"; mkdir -p "$WS/tasks" "$WS/state" "$WORK/elsewhere/tasks"
s="$(start "$WS/tasks" own)"; PIDS+=("$s")
for i in $(seq 1 150); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$s" ] && break; sleep 0.1; done
SENT="$(ls "$WS"/state/*.pid 2>/dev/null | head -1)"
[ -n "$SENT" ] && [ "$(cat "$SENT")" = "$s" ] && [ "$(role_present)" = "yes" ]; check "(setup) the workspace's own watcher $s is ready" $? "$(tail -2 "$WORK/own.err" | tr '\n' '|')"

f="$(start "$WORK/elsewhere/tasks" foreign)"; PIDS+=("$f")
for i in $(seq 1 150); do grep -q 'not stamping' "$WORK/foreign.err" 2>/dev/null && break; sleep 0.1; done
[ "$(cat "$SENT" 2>/dev/null)" = "$s" ]; check "(a) the foreign watcher left the sentinel naming the workspace's watcher" $? "sentinel names $(cat "$SENT" 2>/dev/null), foreign is $f"
[ "$(role_present)" = "yes" ]; check "(a) ...so the workspace's watcher still reads as ready" $?
grep -q "not stamping .*outside .*live watcher pid $s" "$WORK/foreign.err"; check "(a) ...and the foreign watcher said why" $? "$(tail -3 "$WORK/foreign.err" | tr '\n' '|')"

kill -TERM -- "-$f" 2>/dev/null; for i in $(seq 1 50); do alive "$f" || break; sleep 0.1; done
[ "$(cat "$SENT" 2>/dev/null)" = "$s" ] && [ "$(role_present)" = "yes" ]; check "(b) its exit removed nothing: the workspace's watcher is still ready" $? "sentinel: $(cat "$SENT" 2>/dev/null || echo absent)"
alive "$s"; check "(b) ...and was never signalled" $?

# (c) control: with no live holder, a foreign-inbox watcher stamps as before.
kill -TERM -- "-$s" 2>/dev/null; for i in $(seq 1 50); do alive "$s" || break; sleep 0.1; done
rm -f "$WS"/state/*.pid
g="$(start "$WORK/elsewhere/tasks" foreign2)"; PIDS+=("$g")
for i in $(seq 1 150); do [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$g" ] && break; sleep 0.1; done
[ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$g" ]; check "(c) with no live holder the foreign-inbox watcher stamps, as before" $? "$(tail -2 "$WORK/foreign2.err" | tr '\n' '|')"

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

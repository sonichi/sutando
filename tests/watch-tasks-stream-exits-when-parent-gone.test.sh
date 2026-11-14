#!/usr/bin/env bash
# A session watcher whose owning core (nearest claude/codex ancestor) is SIGKILLed
# exits on its own within seconds and releases its sentinel, whether the core is its
# direct parent or sits above a Monitor-style shell wrapper that survives the kill.
# A reused owner pid (new start time) counts as gone; the checker's sleep stops with the
# watcher. With no runtime ancestor it never self-exits, and a standby is never checked.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-ownergone.XXXXXX")"
mkdir -p "$WORK/stubbin"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
# A stand-in core: bash under the name `claude`, which is what the ancestor walk reads.
ln -s "$(command -v bash)" "$WORK/stubbin/claude"
# The real ps, except the owner check's own query reports another start time once $PS_REUSE_FLAG exists.
printf '%s\n' '#!/bin/bash' \
  'if [ -n "${PS_REUSE_FLAG:-}" ] && [ -e "$PS_REUSE_FLAG" ] && [ "$1 $2" = "-o stat=,lstart=" ]; then' \
  "  out=\"\$($(command -v ps) \"\$@\")\" || exit \$?" \
  '  printf "%s\n" "$out" | sed -E "s/[0-9]+:[0-9]+:[0-9]+/00:00:00/"; exit 0' \
  'fi' \
  "exec $(command -v ps) \"\$@\"" > "$WORK/stubbin/ps"
chmod +x "$WORK/stubbin/ps"
PIDS=()
cleanup() {
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -TERM "$p" 2>/dev/null; done
  sleep 0.5
  for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill -KILL "$p" 2>/dev/null; done
  rm -rf "$WORK"
}
trap cleanup EXIT
check() { if [ "$2" = 0 ]; then echo "  PASS $1"; else echo "  FAIL $1${3:+ — $3}"; fail=1; fi; }
alive() { kill -0 "$1" 2>/dev/null; }
# detach <ws> <cmd...>: run cmd double-forked under pid 1, so no ancestor of this test
# (a developer's own claude session included) is in the chain the watcher walks.
detach() {
  local ws="$1"; shift
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_TASKS_DIR -u SUTANDO_WORKSPACE -u SUTANDO_CORE_SESSION \
      SUTANDO_WORKSPACE_DIR="$ws" PATH="$WORK/stubbin:$PATH" W="$WATCHER" WS="$ws" \
      python3 -c '
import os, signal, sys
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # python ignores it, and exec would pass that on
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
os.execvp(sys.argv[1], sys.argv[1:])' "$@" > /dev/null 2>&1 < /dev/null
}
WCMD='bash "$W" "$WS/tasks" --role "$1" --inbox "$WS/tasks" > "$WS/w.out" 2> "$WS/w.err"'
stamped() {  # stamped <ws>: wait for a watcher to stamp its sentinel; prints its pid
  local ws="$1" i w
  for i in $(seq 1 100); do
    w="$(cat "$ws"/state/*.pid 2>/dev/null | head -1)"
    [ -n "$w" ] && alive "$w" && { echo "$w"; return 0; }
    sleep 0.1
  done
  return 1
}
waitfile() { local i; for i in $(seq 1 50); do [ -s "$1" ] && return 0; sleep 0.1; done; return 1; }
gone_within() {  # gone_within <pid> <seconds>
  local i
  for i in $(seq 1 $(( $2 * 10 ))); do alive "$1" || return 0; sleep 0.1; done
  return 1
}
new_ws() { WS="$WORK/$1"; mkdir -p "$WS/tasks" "$WS/state"; }

echo "watch-tasks-stream exits when its owning core is gone:"

# (a) The core is the watcher's direct parent.
new_ws direct
detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; '"$WCMD"' & wait' _ session
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; check "(a) the session watcher started under the core and stamped" $? "$(tail -2 "$WS/w.err" | tr '\n' '|')"
PIDS+=("$w")
fsw="$(pgrep -P "$w" -f fswatch | head -1)"
kill -KILL "$core"
gone_within "$w" 10; check "(a) after kill -9 of the core the watcher exits within 10s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
grep -q "the core that owns this watcher (pid $core) is gone; exiting" "$WS/w.err"; check "(a) ...and names the core on stderr" $?
[ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ]; check "(a) ...and releases its sentinel" $?
{ [ -z "$fsw" ] || gone_within "$fsw" 3; }; check "(a) ...and its fswatch is gone" $?

# (a2) A Monitor-style wrapper between core and watcher: a shell that waits on the
#      watcher (no exec) and outlives the core, so the watcher's own ppid never changes.
new_ws wrapper
detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; sh -c '"'"'echo $$ > "$WS/wrap.pid"; '"$WCMD"'; true'"'"' _ session & wait'
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; PIDS+=("$w")
waitfile "$WS/wrap.pid"; wrap="$(cat "$WS/wrap.pid")"; PIDS+=("$wrap")
[ "$(ps -o ppid= -p "$w" | tr -d ' ')" = "$wrap" ]; check "(a2) the watcher's direct parent is the wrapper, not the core" $?
kill -KILL "$core"
gone_within "$w" 10; check "(a2) after kill -9 of the core the watcher exits within 10s though its wrapper lives" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
[ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ]; check "(a2) ...and releases its sentinel" $?

# (a3) As under a real Monitor, stdout and stderr are a pipe only the core reads: once
#      the core is killed every write there raises SIGPIPE, and shutdown must still finish.
new_ws pipe
mkfifo "$WS/out"
detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; sh -c '"'"'bash "$W" "$WS/tasks" --role session --inbox "$WS/tasks" > "$WS/out" 2>&1; true'"'"' & exec 4< "$WS/out"; wait'
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; check "(a3) the session watcher started with its output piped to the core" $?
PIDS+=("$w")
fsw="$(pgrep -P "$w" -f fswatch | head -1)"
kill -KILL "$core"
gone_within "$w" 10; check "(a3) after kill -9 of the core the watcher exits within 10s though its output pipe is dead" $?
[ -z "$(ls "$WS"/state/*.pid 2>/dev/null)" ]; check "(a3) ...and releases its sentinel" $?
grep -q "the core that owns this watcher (pid $core) is gone; exiting (watcher pid $w)" "$WS/logs/watch-tasks-stream.log" 2>/dev/null; check "(a3) ...and the exit line is on disk in logs/, though stderr's reader is dead" $?
{ [ -z "$fsw" ] || gone_within "$fsw" 3; }; check "(a3) ...and its fswatch is gone" $?

# (b) Core alive: the watcher stays up past several checks.
new_ws alive
detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; '"$WCMD"' & wait' _ session
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; PIDS+=("$w")
sleep 8
alive "$w" && [ "$(cat "$WS"/state/*.pid 2>/dev/null | head -1)" = "$w" ]; check "(b) with its core alive the watcher is still up after 8s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
# The checker's own `sleep 3`, caught just after a fresh one starts so ~3s of it remain.
nap() { local c; for c in $(pgrep -P "$w"); do pgrep -f -x -P "$c" "sleep 3"; done | head -1; }
first="$(nap)"; n="$first"
for i in $(seq 1 50); do n="$(nap)"; [ -n "$n" ] && [ "$n" != "$first" ] && break; sleep 0.1; done
[ -n "$n" ] && [ "$n" != "$first" ]; check "(b) the owner checker started a fresh sleep under the watcher" $?
kill -TERM "$w" 2>/dev/null
gone_within "$n" 1; check "(b) ...and stopping the watcher stops that sleep too, not up to 3s later" $? "sleep $n still alive"
kill -KILL "$core" 2>/dev/null; sleep 0.3

# (f) Core alive but its pid now names another process (a different start time):
#     the watcher treats the owner as gone. ps reports a changed lstart once $PS_REUSE_FLAG exists.
new_ws reuse
PS_REUSE_FLAG="$WS/reused" detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; '"$WCMD"' & wait; exec sleep 60' _ session
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; PIDS+=("$w")
touch "$WS/reused"
gone_within "$w" 10; check "(f) with the core's pid reused (same pid, new start time) the watcher exits within 10s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
alive "$core"; check "(f) ...while that pid itself is still alive" $?
grep -q "the core that owns this watcher (pid $core) is gone; exiting" "$WS/logs/watch-tasks-stream.log" 2>/dev/null; check "(f) ...and logs the exit" $?
kill -KILL "$core" 2>/dev/null; sleep 0.3

# (c) No runtime ancestor, the plain-shell launcher killed: never self-exits.
new_ws noowner
detach "$WS" bash -c 'echo $$ > "$WS/core.pid"; '"$WCMD"' & wait' _ session
waitfile "$WS/core.pid"; launcher="$(cat "$WS/core.pid")"; PIDS+=("$launcher")
w="$(stamped "$WS")"; PIDS+=("$w")
kill -KILL "$launcher"
sleep 8
alive "$w"; check "(c) with no claude/codex ancestor, killing its launcher leaves the watcher up" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
kill -TERM "$w" 2>/dev/null; sleep 0.3

# (d) Launched directly under pid 1 (a subreaper or launchd start): never self-exits.
new_ws pid1
detach "$WS" bash -c "exec $WCMD" _ session
w="$(stamped "$WS")"; PIDS+=("$w")
[ "$(ps -o ppid= -p "$w" | tr -d ' ')" = 1 ]; check "(d) the watcher's parent is pid 1 from the start" $?
sleep 8
alive "$w"; check "(d) ...and it is still up after 8s" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
kill -TERM "$w" 2>/dev/null; sleep 0.3

# (e) A standby is never checked, even under a core that dies.
new_ws standby
detach "$WS" claude -c 'echo $$ > "$WS/core.pid"; '"$WCMD"' & wait' _ standby
waitfile "$WS/core.pid"; core="$(cat "$WS/core.pid")"; PIDS+=("$core")
w="$(stamped "$WS")"; PIDS+=("$w")
kill -KILL "$core"
sleep 8
alive "$w"; check "(e) a standby whose core is SIGKILLed keeps running" $? "$(tail -3 "$WS/w.err" | tr '\n' '|')"
kill -TERM "$w" 2>/dev/null; sleep 0.3

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

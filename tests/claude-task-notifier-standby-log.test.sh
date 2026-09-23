#!/usr/bin/env bash
# The notifier logs when its standby is armed and why it ended: a session
# watcher took the inbox (stood down) or the standby watcher died with none
# ready. These lines are what "how often was the standby needed" is counted from.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
NOTIFIER="$REPO/src/agent/claude/cli/task-notifier.sh"
fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-standby-log.XXXXXX")"
mkdir -p "$WORK/stubbin" "$WORK/ws/tasks" "$WORK/ws/state" "$WORK/ws/logs" "$WORK/ws/results"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$WORK/stubbin/fswatch"; chmod +x "$WORK/stubbin/fswatch"
# No tmux is needed for these lines; a tmux that answers nothing keeps delivery inert.
printf '#!/bin/bash\nexit 1\n' > "$WORK/stubbin/tmux"; chmod +x "$WORK/stubbin/tmux"
LOG="$WORK/ws/logs/claude-task-notifier.log"
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
clean_env() {  # identity stripped; the fixture is the workspace and the inbox
  env -u SUTANDO_INSTANCE_ID -u SUTANDO_AGENT_ID -u AGENT_ID -u AGENT_MXID -u SUTANDO_CORE_SESSION -u SUTANDO_INBOX_KIND -u SUTANDO_INBOX_RESOLVER \
      SUTANDO_WORKSPACE_DIR="$WORK/ws" SUTANDO_TASKS_DIR="$WORK/ws/tasks" SUTANDO_TMUX_SOCKET="$WORK/no.sock" \
      PATH="$WORK/stubbin:$PATH" "$@"
}
start_notifier() {  # prints the pid; its own session (the watcher's cleanup runs kill 0)
  clean_env python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$NOTIFIER" > "$WORK/n.out" 2> "$WORK/n.err" &
  echo $!
}
wait_log() { local i; for i in $(seq 1 100); do grep -q -- "$1" "$LOG" 2>/dev/null && return 0; sleep 0.1; done; return 1; }

echo "task-notifier standby log:"
# (a) armed
n="$(start_notifier)"; PIDS+=("$n")
wait_log "standby armed for $WORK/ws/tasks (standby watcher pid "; check "(a) the notifier logs that its standby is armed, naming the inbox and the watcher pid" $? "$(tail -3 "$WORK/n.err" | tr '\n' '|')"

# (b) a session watcher takes the inbox: the standby watcher yields to it on its
#     own (before the supervisor's TERM, sometimes before the session watcher has
#     stamped), and either ending must read as a stand-down, never as a loss.
s="$(clean_env python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$REPO/src/watch-tasks-stream.sh" "$WORK/ws/tasks" --role session --inbox "$WORK/ws/tasks" > "$WORK/s.out" 2> "$WORK/s.err" & echo $!)"; PIDS+=("$s")
# The standby watcher wrote a sentinel too, and glob order is not deterministic
# across filesystems: ask whether ANY sentinel names the session watcher.
names_session() { grep -qx "$s" "$WORK/ws/state"/*.pid 2>/dev/null; }
for i in $(seq 1 150); do names_session && break; sleep 0.1; done
names_session; check "(b) a session watcher took the inbox (a readiness sentinel names it)" $? "state=$(for f in "$WORK/ws/state"/*.pid; do printf '%s=%s ' "$(basename "$f")" "$(cat "$f")"; done) | $(tail -2 "$WORK/s.err" | tr '\n' '|')"
sleep 1
if grep -q "standby stood down for $WORK/ws/tasks (standby watcher exited)" "$LOG"; then
  echo "  PASS (b) the standby watcher yielded on its own and the log says it stood down"
else
  kill -TERM -- "-$n" 2>/dev/null || kill -TERM "$n" 2>/dev/null   # what the supervisor does on 'yes'
  wait_log "standby stood down for $WORK/ws/tasks (stopped by signal): a session-role watcher is ready"; check "(b) when stopped while a session watcher is ready, the log says the standby stood down" $? "log: $(sed 's/^.*task-notifier: //' "$LOG" | tr '\n' '|') | n.err: $(tail -2 "$WORK/n.err" | tr '\n' '|')"
fi
! grep -q "with no session-role watcher ready" "$LOG"; check "(b) ...and the hand-off was never logged as a loss" $? "log: $(sed 's/^.*task-notifier: //' "$LOG" | tr '\n' '|')"
for i in $(seq 1 50); do alive "$n" || break; sleep 0.1; done
! alive "$n"; check "(b) ...and the notifier exits" $?
kill -TERM -- "-$s" 2>/dev/null; kill -TERM "$s" 2>/dev/null; wait "$s" 2>/dev/null; sleep 0.5
rm -f "$WORK/ws/state"/*.pid

# (c) the standby watcher dies with no session watcher ready: the log says so
: > "$LOG"
n="$(start_notifier)"; PIDS+=("$n")
wait_log "standby armed for"
wp="$(sed -n 's/.*standby watcher pid \([0-9]*\)).*/\1/p' "$LOG" | tail -1)"
[ -n "$wp" ] && kill -KILL "$wp" 2>/dev/null
wait_log "standby ended for $WORK/ws/tasks (standby watcher exited) with no session-role watcher ready"; check "(c) when the standby watcher dies with no session watcher, the log says so (not 'stood down')" $? "log: $(sed 's/^.*task-notifier: //' "$LOG" | tr '\n' '|')"
! grep -q "stood down" "$LOG"; check "(c) ...and no false 'stood down' line" $?

echo
if [ "$fail" = 0 ]; then echo "ALL TESTS PASS"; else echo "TESTS FAILED"; exit 1; fi

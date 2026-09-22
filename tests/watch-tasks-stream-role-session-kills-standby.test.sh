#!/usr/bin/env bash
# The hosting-mode handoff contract, with the real supervisor, the real
# notifier and its real standby watcher, on a scratch tmux socket:
#   (a) the standby pair stops only AFTER the session watcher's sentinel exists;
#   (b) the supervisor is alive after the handoff (it is the only re-arm);
#   (c) exactly one sentinel remains and it names the session watcher;
#   (d) a session watcher whose fswatch dies before readiness leaves the
#       standby, its sentinel and the supervisor untouched;
#   (e) a session watcher that dies after the handoff is replaced: the
#       supervisor re-arms the pair within grace + poll (the gap is printed);
#   (f) a readiness probe that never comes back exits the session watcher,
#       standby untouched.
#
# Run: bash tests/watch-tasks-stream-role-session-kills-standby.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
WATCHER="$REPO/src/watch-tasks-stream.sh"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-handoff.XXXXXX")"
SOCK="$WORK/tmux.sock"
mkdir -p "$WORK/tasks" "$WORK/state" "$WORK/results"
GRACE=2
POLL=1

# Real events on any host: a polling stand-in for fswatch, unless the caller
# asks for the host's own fswatch (SUTANDO_TEST_REAL_FSWATCH=1: the live witness).
STUBBIN="$WORK/stubbin"
mkdir -p "$STUBBIN"
cp "$REPO/tests/fixtures/fswatch-poll-stub.sh" "$STUBBIN/fswatch"
chmod +x "$STUBBIN/fswatch"
if [ "${SUTANDO_TEST_REAL_FSWATCH:-0}" = "1" ] && command -v fswatch >/dev/null 2>&1; then
  echo "  using the host's fswatch: $(command -v fswatch)"
else
  export PATH="$STUBBIN:$PATH"
fi
# fswatch that dies after the liveness check but before any event.
DIEBIN="$WORK/diebin"
mkdir -p "$DIEBIN"
printf '#!/bin/bash\nsleep 0.7\nexit 1\n' > "$DIEBIN/fswatch"
chmod +x "$DIEBIN/fswatch"
# fswatch that runs and never emits.
IDLEBIN="$WORK/idlebin"
mkdir -p "$IDLEBIN"
printf '#!/bin/bash\nexec sleep 100000\n' > "$IDLEBIN/fswatch"
chmod +x "$IDLEBIN/fswatch"

sup_pid=""
cleanup_all() {
  # The notifier runs in its own process group under the supervisor; end that
  # group by pid, never by name, so a real notifier on this host is untouched.
  local c
  for c in $(pgrep -P "${sup_pid:-0}" 2>/dev/null); do
    kill -9 -- "-$c" 2>/dev/null || kill -9 "$c" 2>/dev/null || true
  done
  tmux -S "$SOCK" kill-server >/dev/null 2>&1 || true
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

# The core pane the notifier types into: it shows the idle footer and sits there.
tmux -S "$SOCK" new-session -d -s target -c "$REPO" \
  "printf '\n⏵⏵ bypass permissions on\n'; sleep 100000"

# The supervisor, hosted the way the launcher hosts it: in "${SESSION}-watcher".
tmux -S "$SOCK" new-session -d -s target-watcher -c "$REPO" \
  "env -u SUTANDO_INSTANCE_ID SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target \
     SUTANDO_TASKS_DIR=$WORK/tasks SUTANDO_WORKSPACE_DIR=$WORK \
     SUTANDO_NOTIFIER_GRACE_PERIOD=$GRACE SUTANDO_NOTIFIER_ROLE_POLL=$POLL SUTANDO_NOTIFIER_TARGET_POLL=$POLL \
     bash $SUPERVISOR > $WORK/sup.log 2>&1"

supervisor_pid() { pgrep -f "bash $SUPERVISOR" | head -1; }
sentinel_pid() { cat "$WORK"/state/*.pid 2>/dev/null | head -1; }
sentinel_count() { ls "$WORK"/state/*.pid 2>/dev/null | wc -l | tr -d ' '; }
notifier_pid() { pgrep -f "task-notifier.sh" | while read -r p; do
  ps -o ppid= -p "$p" 2>/dev/null | grep -q . && echo "$p"; done | head -1; }
alive() { kill -0 "$1" 2>/dev/null; }
now_ms() { python3 -c 'import time; print(int(time.time()*1000))'; }

wait_standby() {  # until a standby watcher stamped a sentinel; prints its pid
  local i
  for i in $(seq 1 150); do
    if [ -n "$(sentinel_pid)" ] && alive "$(sentinel_pid)"; then echo "$(sentinel_pid)"; return 0; fi
    sleep 0.1
  done
  return 1
}

start_session_watcher() {  # start_session_watcher <name> [<PATH prefix>] [<ready timeout>]
  local name="$1" extra_path="${2:-}" ready="${3:-10}"
  tmux -S "$SOCK" new-session -d -s "$name" -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID PATH=${extra_path:+$extra_path:}\$PATH \
       SUTANDO_WORKSPACE_DIR=$WORK SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target \
       SUTANDO_WATCHER_READY_TIMEOUT=$ready \
       bash $WATCHER $WORK/tasks --role session --inbox $WORK/tasks > $WORK/$name.log 2> $WORK/$name.err"
}

standby_pid="$(wait_standby)" || { echo "  FAIL: setup -- the supervisor never armed a standby watcher"; fail=1; }
sup_pid="$(supervisor_pid)"
[ -n "$sup_pid" ] || { echo "  FAIL: setup -- no supervisor process"; fail=1; }

if [ "$fail" -eq 0 ]; then
  # --- (a)(b)(c): the handoff ------------------------------------------------
  start_session_watcher ses
  t_sentinel=""; t_standby_gone=""; t0="$(now_ms)"
  for i in $(seq 1 300); do
    sp="$(sentinel_pid)"
    if [ -z "$t_sentinel" ] && [ -n "$sp" ] && [ "$sp" != "$standby_pid" ]; then t_sentinel="$(now_ms)"; ses_pid="$sp"; fi
    if [ -z "$t_standby_gone" ] && ! alive "$standby_pid"; then t_standby_gone="$(now_ms)"; fi
    [ -n "$t_sentinel" ] && [ -n "$t_standby_gone" ] && break
    sleep 0.1
  done
  if [ -n "$t_sentinel" ] && [ -n "$t_standby_gone" ] && [ "$t_standby_gone" -ge "$t_sentinel" ]; then
    echo "  PASS (a): the standby stopped only after the session sentinel existed (sentinel +$((t_sentinel - t0)) ms, standby gone +$((t_standby_gone - t0)) ms)"
  else
    echo "  FAIL (a): sentinel at '${t_sentinel:-never}', standby gone at '${t_standby_gone:-never}' (ms since start $t0)"
    fail=1
  fi
  if alive "$sup_pid"; then
    echo "  PASS (b): the supervisor ($sup_pid) is alive after the handoff"
  else
    echo "  FAIL (b): the supervisor died during the handoff"
    fail=1
  fi
  sleep 1
  if [ "$(sentinel_count)" = "1" ] && [ "$(sentinel_pid)" = "${ses_pid:-}" ] && alive "${ses_pid:-0}"; then
    echo "  PASS (c): exactly one sentinel remains and it names the session watcher (${ses_pid:-})"
  else
    echo "  FAIL (c): expected 1 sentinel naming the live session watcher, found $(sentinel_count) naming '$(sentinel_pid)'"
    fail=1
  fi

  # --- (e): post-takeover death restores coverage -----------------------------
  fsw="$(pgrep -P "${ses_pid:-0}" -f fswatch | head -1)"
  t_kill="$(now_ms)"
  [ -n "$fsw" ] && kill -9 "$fsw" 2>/dev/null
  new_standby=""
  for i in $(seq 1 400); do
    sp="$(sentinel_pid)"
    if [ -n "$sp" ] && [ "$sp" != "${ses_pid:-}" ] && alive "$sp"; then new_standby="$sp"; break; fi
    sleep 0.1
  done
  if [ -n "$new_standby" ]; then
    gap=$(( $(now_ms) - t_kill ))
    if [ "$gap" -le $(( (GRACE + 3 * POLL + 5) * 1000 )) ]; then
      echo "  PASS (e): after the session watcher's fswatch died, the supervisor re-armed a standby ($new_standby) in ${gap} ms (grace ${GRACE}s, poll ${POLL}s)"
    else
      echo "  FAIL (e): re-arm took ${gap} ms, more than grace + polls"
      fail=1
    fi
  else
    echo "  FAIL (e): no standby re-armed after the session watcher died"
    fail=1
  fi
  tmux -S "$SOCK" kill-session -t ses >/dev/null 2>&1 || true
  if alive "$sup_pid"; then
    echo "  PASS (b'): the supervisor is still the same process ($sup_pid) after the re-arm"
  else
    echo "  FAIL (b'): the supervisor was replaced or died"
    fail=1
  fi

  # --- (d): fswatch dies before readiness --------------------------------------
  standby_pid="$new_standby"
  start_session_watcher ses-die "$DIEBIN"
  gone=""
  for i in $(seq 1 100); do
    tmux -S "$SOCK" has-session -t ses-die 2>/dev/null || { gone=1; break; }
    sleep 0.1
  done
  if [ -n "$gone" ] && alive "$standby_pid" && [ "$(sentinel_pid)" = "$standby_pid" ] && [ "$(sentinel_count)" = "1" ] && alive "$sup_pid"; then
    echo "  PASS (d): fswatch died before readiness: session watcher exited, standby ($standby_pid) and its sentinel untouched, supervisor alive"
  else
    echo "  FAIL (d): gone=${gone:-0} standby-alive=$(alive "$standby_pid" && echo yes || echo no) sentinel=$(sentinel_pid) count=$(sentinel_count) supervisor-alive=$(alive "$sup_pid" && echo yes || echo no)"
    fail=1
  fi
  grep -q "no sentinel written and no standby touched" "$WORK/ses-die.err" 2>/dev/null \
    && echo "  PASS (d'): the failed arm says why it left the standby alone" \
    || { echo "  FAIL (d'): no explanation line in the session watcher's stderr"; fail=1; }

  # --- (f): the readiness probe never comes back -----------------------------
  start_session_watcher ses-idle "$IDLEBIN" 2
  gone=""
  for i in $(seq 1 100); do
    tmux -S "$SOCK" has-session -t ses-idle 2>/dev/null || { gone=1; break; }
    sleep 0.1
  done
  if [ -n "$gone" ] && alive "$standby_pid" && [ "$(sentinel_pid)" = "$standby_pid" ] && grep -q "no event came back" "$WORK/ses-idle.err" 2>/dev/null; then
    echo "  PASS (f): a probe that never returns exits the session watcher; standby ($standby_pid) untouched"
  else
    echo "  FAIL (f): gone=${gone:-0} standby-alive=$(alive "$standby_pid" && echo yes || echo no) sentinel=$(sentinel_pid); stderr: $(tail -1 "$WORK/ses-idle.err" 2>/dev/null)"
    fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "PASSED: the hosting-mode handoff keeps the supervisor alive and stands the standby down only on readiness"
else
  echo "FAILED: the hosting-mode handoff keeps the supervisor alive and stands the standby down only on readiness"
fi
exit "$fail"

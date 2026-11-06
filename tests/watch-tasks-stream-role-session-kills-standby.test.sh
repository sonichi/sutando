#!/usr/bin/env bash
# The hosting-mode handoff contract, with the real supervisor, the real
# notifier and its real standby watcher, on a scratch tmux socket:
#   (a) the standby pair stops only AFTER the session watcher's sentinel exists;
#   (b) the supervisor is alive after the handoff (it is the only re-arm);
#   (c) exactly one sentinel remains and it names the session watcher;
#   (d) a session watcher whose fswatch dies before readiness leaves the
#       standby, its sentinel and the supervisor untouched;
#   (e0) a session watcher outlives its fswatch: the child is relaunched, the
#       sentinel stands and no standby arms;
#   (e) a session watcher that dies after the handoff is replaced: the
#       supervisor logs the death and re-arms the pair within grace + poll
#       (the gap is printed); (e2) the same for a death with no cleanup
#       (SIGKILL), whose stale sentinel the standby overwrites;
#   (f) a readiness probe that never comes back exits the session watcher,
#       standby untouched;
#   (g) an inbox named through a symlinked path with no workspace override
#       is still recognised as ready by the supervisor;
#   (z) teardown leaves nothing naming either scratch dir, the setsid'd standby
#       included: it is in no group but its own, and it relaunches a killed fswatch.
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
WORK2=""
# Every kill below is gated on these: an empty pid means `pgrep -P 0`, which on Linux
# lists init, and an empty pattern matches every process on the host.
is_rig_supervisor() { case "$(ps -o command= -p "${1:-0}" 2>/dev/null)" in "bash $SUPERVISOR") return 0 ;; esac; return 1; }
is_tag() { case "$1" in sut-handoff.??????|sut-handoff-link.??????) return 0 ;; esac; return 1; }
# By pid and group, never by name: the supervisor stopped first so it re-arms nothing,
# then the notifier's group, tmux, and each watcher a sentinel names, in its own group.
stop_rig() {  # <supervisor pid> <tmux socket> <state dir> <tag: the work dir's basename>
  local p g
  is_tag "$4" || return 0
  if is_rig_supervisor "$1"; then
    kill -STOP "$1" 2>/dev/null
    for p in $(pgrep -P "$1" 2>/dev/null); do
      kill -9 -- "-$p" 2>/dev/null || kill -9 "$p" 2>/dev/null || true
    done
    kill -9 "$1" 2>/dev/null
  fi
  tmux -S "$2" kill-server >/dev/null 2>&1 || true
  for p in $(cat "$3"/*.pid 2>/dev/null); do
    case "$p" in ''|*[!0-9]*|0|1) continue ;; esac
    ps -o command= -p "$p" 2>/dev/null | grep -qF "$4" || continue
    pkill -9 -P "$p" 2>/dev/null
    g="$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')"
    if [ "$g" = "$p" ]; then kill -9 -- "-$p" 2>/dev/null; else kill -9 "$p" 2>/dev/null; fi
  done
}
rig_leftovers() {  # <tag>: what still names it after stop_rig, once exits have had 5 s to land
  local i left
  is_tag "$1" || { echo "not a scratch-dir tag: '$1'"; return; }
  for i in $(seq 1 50); do
    left="$(pgrep -f "$1" 2>/dev/null | grep -vx "$$")"
    [ -z "$left" ] && return 0
    sleep 0.1
  done
  for i in $left; do ps -o pid=,command= -p "$i" 2>/dev/null; done
}
sweep() {  # <tag>: the last resort, anything whose command line names it
  local p
  is_tag "$1" || return 0
  for p in $(pgrep -f "$1" 2>/dev/null); do [ "$p" = "$$" ] || kill -9 "$p" 2>/dev/null; done
}
cleanup_all() {
  if [ -n "$WORK2" ]; then
    stop_rig "${sup2_pid:-}" "$SOCK2" "$WORK2/real/state" "$(basename "$WORK2")"
    sweep "$(basename "$WORK2")"
    rm -rf "$WORK2"
  fi
  stop_rig "$sup_pid" "$SOCK" "$WORK/state" "$(basename "$WORK")"
  sweep "$(basename "$WORK")"
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

rig_supervisor_pid() {  # <tmux socket>: the supervisor in that rig's own pane, no other
  local pane p
  pane="$(tmux -S "$1" display -p -t target-watcher '#{pane_pid}' 2>/dev/null)"
  case "$pane" in ''|*[!0-9]*) return ;; esac
  for p in "$pane" $(pgrep -P "$pane" 2>/dev/null); do
    is_rig_supervisor "$p" && { echo "$p"; return; }
  done
}
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
sup_pid="$(rig_supervisor_pid "$SOCK")"
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

  # --- (e0): the session watcher outlives its fswatch ----------------------------
  fsw="$(pgrep -P "${ses_pid:-0}" -f fswatch | head -1)"
  [ -n "$fsw" ] && kill -9 "$fsw" 2>/dev/null
  new_fsw=""
  for i in $(seq 1 80); do
    new_fsw="$(pgrep -P "${ses_pid:-0}" -f fswatch | head -1)"
    [ -n "$new_fsw" ] && [ "$new_fsw" != "$fsw" ] && break
    new_fsw=""
    sleep 0.1
  done
  sleep "$GRACE"   # long enough for a supervisor that misread the death to arm
  if alive "${ses_pid:-0}" && [ -n "$new_fsw" ] && [ "$(sentinel_pid)" = "${ses_pid:-}" ] && [ "$(sentinel_count)" = "1" ]; then
    echo "  PASS (e0): the session watcher outlived its fswatch ($fsw -> $new_fsw); its sentinel stands and no standby armed"
  else
    echo "  FAIL (e0): after fswatch $fsw died: watcher-alive=$(alive "${ses_pid:-0}" && echo yes || echo no) new-fswatch='${new_fsw}' sentinel=$(sentinel_pid) count=$(sentinel_count); stderr: $(tail -2 "$WORK/ses.err" 2>/dev/null)"
    fail=1
  fi

  # --- (e): the session watcher's own death restores coverage --------------------
  t_kill="$(now_ms)"
  kill -TERM "${ses_pid:-0}" 2>/dev/null
  new_standby=""
  for i in $(seq 1 400); do
    sp="$(sentinel_pid)"
    if [ -n "$sp" ] && [ "$sp" != "${ses_pid:-}" ] && alive "$sp"; then new_standby="$sp"; break; fi
    sleep 0.1
  done
  if [ -n "$new_standby" ]; then
    gap=$(( $(now_ms) - t_kill ))
    if [ "$gap" -le $(( (GRACE + 3 * POLL + 5) * 1000 )) ]; then
      echo "  PASS (e): after the session watcher was SIGTERMed, the supervisor re-armed a standby ($new_standby) in ${gap} ms (grace ${GRACE}s, poll ${POLL}s)"
    else
      echo "  FAIL (e): re-arm took ${gap} ms, more than grace + polls"
      fail=1
    fi
  else
    echo "  FAIL (e): no standby re-armed after the session watcher died"
    fail=1
  fi
  if grep -q "the session watcher for .* is gone" "$WORK/sup.log" 2>/dev/null; then
    echo "  PASS (e'): the supervisor logged the death and the clock its re-arm runs on"
  else
    echo "  FAIL (e'): no death line in the supervisor's log: $(tail -2 "$WORK/sup.log" 2>/dev/null)"
    fail=1
  fi
  tmux -S "$SOCK" kill-session -t ses >/dev/null 2>&1 || true
  if alive "$sup_pid"; then
    echo "  PASS (b'): the supervisor is still the same process ($sup_pid) after the re-arm"
  else
    echo "  FAIL (b'): the supervisor was replaced or died"
    fail=1
  fi

  # --- (e2): a death with no cleanup leaves a stale sentinel; the standby still comes
  standby_pid="$new_standby"
  start_session_watcher ses2
  ses2_pid=""
  for i in $(seq 1 300); do
    sp="$(sentinel_pid)"
    if [ -n "$sp" ] && [ "$sp" != "$standby_pid" ] && alive "$sp" && ! alive "$standby_pid"; then ses2_pid="$sp"; break; fi
    sleep 0.1
  done
  if [ -z "$ses2_pid" ]; then
    echo "  FAIL (e2): setup -- a second session watcher never took the inbox over from standby $standby_pid"
    fail=1
  else
    t_kill="$(now_ms)"
    kill -9 "$ses2_pid" 2>/dev/null
    new_standby=""
    for i in $(seq 1 400); do
      sp="$(sentinel_pid)"
      if [ -n "$sp" ] && [ "$sp" != "$ses2_pid" ] && alive "$sp"; then new_standby="$sp"; break; fi
      sleep 0.1
    done
    gap=$(( $(now_ms) - t_kill ))
    if [ -n "$new_standby" ] && [ "$gap" -le $(( (GRACE + 3 * POLL + 5) * 1000 )) ] && [ "$(sentinel_count)" = "1" ]; then
      echo "  PASS (e2): after the session watcher was SIGKILLed (stale sentinel $ses2_pid), a standby ($new_standby) overwrote it in ${gap} ms"
    else
      echo "  FAIL (e2): SIGKILLed session watcher $ses2_pid: standby='${new_standby}' after ${gap} ms, sentinel=$(sentinel_pid) count=$(sentinel_count)"
      fail=1
    fi
    tmux -S "$SOCK" kill-session -t ses2 >/dev/null 2>&1 || true
  fi

  # --- (d): fswatch dies before readiness --------------------------------------
  standby_pid="${new_standby:-$standby_pid}"
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

# --- (g): an inbox named through a symlinked path, no workspace override --------
# The watcher stamps under the physical inbox's parent; the supervisor must
# look there too, or a session watcher is never seen as ready.
WORK2="$(mktemp -d "${TMPDIR:-/tmp}/sut-handoff-link.XXXXXX")"
SOCK2="$WORK2/tmux.sock"
mkdir -p "$WORK2/real/tasks" "$WORK2/real/state" "$WORK2/real/results" "$WORK2/alias"
ln -s "$WORK2/real/tasks" "$WORK2/alias/tasks"
tmux -S "$SOCK2" new-session -d -s target -c "$REPO" \
  "printf '\n⏵⏵ bypass permissions on\n'; sleep 100000"
tmux -S "$SOCK2" new-session -d -s target-watcher -c "$REPO" \
  "env -u SUTANDO_INSTANCE_ID -u SUTANDO_WORKSPACE_DIR SUTANDO_TMUX_SOCKET=$SOCK2 SUTANDO_TMUX_SESSION=target \
     SUTANDO_TASKS_DIR=$WORK2/alias/tasks \
     SUTANDO_NOTIFIER_GRACE_PERIOD=$GRACE SUTANDO_NOTIFIER_ROLE_POLL=$POLL SUTANDO_NOTIFIER_TARGET_POLL=$POLL \
     bash $SUPERVISOR > $WORK2/sup.log 2>&1"
sup2_pid=""
sentinel2_pid() { cat "$WORK2"/real/state/*.pid 2>/dev/null | head -1; }
sb2=""
for i in $(seq 1 150); do
  sb2="$(sentinel2_pid)"
  [ -n "$sb2" ] && alive "$sb2" && break
  sleep 0.1
done
sup2_pid="$(rig_supervisor_pid "$SOCK2")"
if [ -z "$sb2" ]; then
  echo "  FAIL (g): setup -- the supervisor on the symlinked inbox never armed a standby (sentinel dir $WORK2/real/state)"
  fail=1
else
  tmux -S "$SOCK2" new-session -d -s ses-link -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID -u SUTANDO_WORKSPACE_DIR SUTANDO_TMUX_SOCKET=$SOCK2 SUTANDO_TMUX_SESSION=target \
       bash $WATCHER $WORK2/alias/tasks --role session --inbox $WORK2/alias/tasks > $WORK2/ses.log 2> $WORK2/ses.err"
  gone=""
  for i in $(seq 1 100); do
    alive "$sb2" || { gone=1; break; }
    sleep 0.1
  done
  s2="$(sentinel2_pid)"
  if [ -n "$gone" ] && [ -n "$s2" ] && [ "$s2" != "$sb2" ] && alive "$s2" && [ "$(ls "$WORK2"/real/state/*.pid | wc -l | tr -d ' ')" = "1" ]; then
    echo "  PASS (g): symlinked inbox, no workspace override: the standby stood down and one sentinel names the session watcher ($s2)"
  else
    echo "  FAIL (g): symlinked inbox: standby-gone=${gone:-0} sentinel=$s2 (standby was $sb2): the supervisor never saw the session watcher as ready"
    fail=1
  fi
fi

# --- (z): teardown leaves nothing behind -------------------------------------
# The symlinked rig first: its fswatch runs from the first rig's stub dir, so names both.
for rig in "$sup2_pid|$SOCK2|$WORK2/real/state|$WORK2" "$sup_pid|$SOCK|$WORK/state|$WORK"; do
  IFS='|' read -r r_sup r_sock r_state r_work <<< "$rig"
  tag="$(basename "$r_work")"
  stop_rig "$r_sup" "$r_sock" "$r_state" "$tag"
  left="$(rig_leftovers "$tag")"
  if [ -z "$left" ]; then
    echo "  PASS (z): nothing names $tag after its teardown (supervisor ${r_sup:-none}, its notifier's group, tmux, each sentinel's watcher)"
  else
    echo "  FAIL (z): still running after the teardown of $tag, swept now: $left"
    fail=1
  fi
  sweep "$tag"
done
sup_pid=""; sup2_pid=""

if [ "$fail" -eq 0 ]; then
  echo "PASSED: the hosting-mode handoff keeps the supervisor alive and stands the standby down only on readiness"
else
  echo "FAILED: the hosting-mode handoff keeps the supervisor alive and stands the standby down only on readiness"
fi
exit "$fail"

#!/usr/bin/env bash
# Regression guard for task-notifier-supervisor.sh's standby/grace-period
# gate (#4477): the external notifier must not run alongside an in-session
# (--role session) watcher for the SAME inbox, must arm after a grace period
# with none seen, must disarm the moment one appears, must stay disarmed
# while it persists, must ignore a session-role watcher for a DIFFERENT
# inbox, and must treat an unobservable `ps` snapshot as "unknown", not "no
# watcher": no arming inside the grace period, arming after it rather than
# leaving the inbox unwatched forever.
#
# Uses a stub notifier (SUTANDO_NOTIFIER_SCRIPT override) so this test does
# not depend on the real task-notifier.sh/pane machinery, which is out of
# scope for this change. Real tmux, real watch-tasks-stream.sh processes,
# real watcher_identity.py role-present calls -- the actual code under test,
# not a reimplementation of it.
#
# Run: bash tests/task-notifier-supervisor-standby-gate.test.sh
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
WATCHER="$REPO/src/watch-tasks-stream.sh"

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not available"; exit 0; }

fail=0
WORK="$(mktemp -d "${TMPDIR:-/tmp}/sut-sup-gate.XXXXXX")"
SOCK="$WORK/tmux.sock"

STUB="$WORK/stub-notifier.sh"
cat > "$STUB" <<'EOS'
#!/bin/bash
echo "$$" > "$SUTANDO_STUB_MARKER"
trap 'rm -f "$SUTANDO_STUB_MARKER"; exit 0' TERM
while true; do sleep 1; done
EOS
chmod +x "$STUB"

# The watchers' event source is not under test here, only whether they are
# seen; a stub fswatch that idles keeps them alive on a host that ships none.
STUBBIN="$WORK/stubbin"
mkdir -p "$STUBBIN"
printf '#!/bin/bash\nexec sleep 100000\n' > "$STUBBIN/fswatch"
chmod +x "$STUBBIN/fswatch"
export PATH="$STUBBIN:$PATH"

cleanup_all() {
  tmux -S "$SOCK" kill-server >/dev/null 2>&1 || true
  pkill -9 -f "fswatch.*$WORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup_all EXIT

start_target() { tmux -S "$SOCK" new-session -d -s target -c "$REPO" "sleep 600"; }

start_supervisor() {
  local tasks_dir="$1" marker="$2" grace="$3" role_poll="$4" extra_path="${5:-}"
  tmux -S "$SOCK" new-session -d -s supervisor -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID PATH=${extra_path:+$extra_path:}\$PATH \
       SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target \
       SUTANDO_TASKS_DIR=$tasks_dir SUTANDO_NOTIFIER_SCRIPT=$STUB \
       SUTANDO_STUB_MARKER=$marker SUTANDO_NOTIFIER_GRACE_PERIOD=$grace \
       SUTANDO_NOTIFIER_ROLE_POLL=$role_poll \
       bash $SUPERVISOR > $WORK/sup.log 2>&1"
}

start_internal_watcher() {
  local tasks_dir="$1" name="$2"
  tmux -S "$SOCK" new-session -d -s "$name" -c "$REPO" \
    "env -u SUTANDO_INSTANCE_ID SUTANDO_TMUX_SOCKET=$SOCK SUTANDO_TMUX_SESSION=target \
       bash $WATCHER $tasks_dir --role session --inbox $tasks_dir > $WORK/$name.log 2>&1"
}

wait_for() {  # wait_for <file> <timeout-tenths> -- true if the file becomes non-empty
  local f="$1" n="$2" i
  for i in $(seq 1 "$n"); do
    [ -s "$f" ] && return 0
    sleep 0.1
  done
  return 1
}

wait_for_absent() {  # wait_for_absent <file> <timeout-tenths> -- true if the file becomes empty/gone
  local f="$1" n="$2" i
  for i in $(seq 1 "$n"); do
    [ -s "$f" ] || return 0
    sleep 0.1
  done
  return 1
}

# --- scenario 1: no session-role watcher -> arms after the grace period ----
mkdir -p "$WORK/core/tasks" "$WORK/state"
start_target
MARK1="$WORK/notifier1.marker"
start_supervisor "$WORK/core/tasks" "$MARK1" 3 1
if wait_for "$MARK1" 100; then
  echo "  PASS: scenario 1 (core inbox) -- notifier armed with no session watcher present"
else
  echo "  FAIL: scenario 1 -- notifier never armed within timeout"
  fail=1
fi

# --- scenario 2: a session-role watcher for the SAME inbox -> disarms ------
start_internal_watcher "$WORK/core/tasks" internal-core
if wait_for_absent "$MARK1" 100; then
  echo "  PASS: scenario 2 -- notifier disarmed once the session-role watcher for its own inbox appeared"
else
  echo "  FAIL: scenario 2 -- notifier still running after a same-inbox session watcher appeared"
  fail=1
fi

# --- scenario 3: stays disarmed while the session watcher persists ---------
sleep 4
if [ ! -s "$MARK1" ]; then
  echo "  PASS: scenario 3 -- no re-arm while the session-role watcher is still up"
else
  echo "  FAIL: scenario 3 -- re-armed even though the session-role watcher is still present"
  fail=1
fi
tmux -S "$SOCK" kill-session -t supervisor >/dev/null 2>&1 || true
tmux -S "$SOCK" kill-session -t internal-core >/dev/null 2>&1 || true
sleep 0.3

# --- scenario 4: a DIFFERENT inbox's session watcher does not disarm this --
mkdir -p "$WORK/worker/tasks"
MARK2="$WORK/notifier2.marker"
start_supervisor "$WORK/worker/tasks" "$MARK2" 2 1
if ! wait_for "$MARK2" 100; then
  echo "  FAIL: scenario 4 setup -- worker-inbox notifier never armed"
  fail=1
else
  start_internal_watcher "$WORK/core/tasks" internal-core2
  sleep 5
  if [ -s "$MARK2" ]; then
    echo "  PASS: scenario 4 -- worker-inbox notifier unaffected by core's session watcher"
  else
    echo "  FAIL: scenario 4 -- worker-inbox notifier disarmed by a DIFFERENT inbox's session watcher"
    fail=1
  fi
fi
tmux -S "$SOCK" kill-session -t supervisor >/dev/null 2>&1 || true
tmux -S "$SOCK" kill-session -t internal-core2 >/dev/null 2>&1 || true
sleep 0.3

# --- scenario 5: an unobservable `ps` is not "clear to arm" -- it gets the
# --- same grace period a clean "no" gets, then arms rather than leaving the
# --- inbox with no watcher forever.
FAKE_PS_DIR="$WORK/fakebin"
mkdir -p "$FAKE_PS_DIR"
cat > "$FAKE_PS_DIR/ps" <<'EOS'
#!/bin/bash
exit 1
EOS
chmod +x "$FAKE_PS_DIR/ps"
MARK3="$WORK/notifier3.marker"
start_supervisor "$WORK/core/tasks" "$MARK3" 6 1 "$FAKE_PS_DIR"
sleep 2
if [ ! -s "$MARK3" ]; then
  echo "  PASS: scenario 5a -- an unobservable ps snapshot did not arm the notifier inside the grace period"
else
  echo "  FAIL: scenario 5a -- notifier armed at once on an unobservable ps snapshot (unknown treated as clear)"
  fail=1
fi
if wait_for "$MARK3" 120; then
  echo "  PASS: scenario 5b -- an unobservable ps that persists past the grace period arms rather than never"
else
  echo "  FAIL: scenario 5b -- notifier never armed while ps stayed unobservable (an inbox with no watcher, forever)"
  fail=1
fi
tmux -S "$SOCK" kill-session -t supervisor >/dev/null 2>&1 || true

if [ "$fail" -eq 0 ]; then
  echo "PASSED: task-notifier-supervisor standby gate"
else
  echo "FAILED: task-notifier-supervisor standby gate"
fi
exit "$fail"

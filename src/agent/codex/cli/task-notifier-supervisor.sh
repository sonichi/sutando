#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session
# lives -- but ONLY while no in-session (--role session) watcher already
# covers this inbox. The two hosting modes (a live session's own Monitor
# watcher, vs this supervisor's external notifier+watcher) are mutually
# exclusive by code now, not by an agent instruction: this script
# starts in standby, arms after a grace period with no session-role watcher
# seen for its inbox, and disarms the moment one appears.
set -u

REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
# shellcheck source=../../../tasks-dir-resolve.sh
source "$REPO/src/tasks-dir-resolve.sh"
# No positional inbox arg for a supervisor invocation -- SUTANDO_TASKS_DIR (if
# set) or the canonical loader, same as the watcher it's standing in for would
# resolve. A resolution failure leaves TASKS_DIR empty: role_present() then
# runs host-wide (no --inbox), same as without an inbox tag -- degrade, don't abort.
TASKS_DIR="$(resolve_tasks_dir "${SUTANDO_TASKS_DIR:-}" "$REPO")" || TASKS_DIR=""
PY="${SUTANDO_NOTIFIER_PY:-python3}"
WATCHER_IDENTITY="$REPO/src/watcher_identity.py"

# The exact core pane, not the session: a sibling window keeps a session alive
# after the core is gone, and a watcher must not outlive its target.
# With a declared window the gate is that pane; undeclared keeps the session gate.
# A declared pane is the exact target (an index can be reused by a replacement);
# a declared window is the next best; undeclared keeps the session gate.
target_alive() {
  if [ -n "${SUTANDO_TMUX_PANE:-}" ]; then
    [ "$(tmux -S "$TMUX_SOCKET" display-message -p -t "$SUTANDO_TMUX_PANE" '#{pane_id}' 2>/dev/null)" = "$SUTANDO_TMUX_PANE" ]
  elif [ -n "${SUTANDO_TMUX_WINDOW:-}" ]; then
    tmux -S "$TMUX_SOCKET" list-panes -t "=$SESSION:$SUTANDO_TMUX_WINDOW" >/dev/null 2>&1
  else
    tmux -S "$TMUX_SOCKET" has-session -t "=$SESSION" 2>/dev/null
  fi
}
declared_target() { [ -n "${SUTANDO_TMUX_PANE:-}${SUTANDO_TMUX_WINDOW:-}" ]; }
RESTART_DELAY="${SUTANDO_NOTIFIER_RESTART_DELAY:-1}"
TARGET_POLL="${SUTANDO_NOTIFIER_TARGET_POLL:-2}"
NOTIFIER="${SUTANDO_NOTIFIER_SCRIPT:-$REPO/src/agent/codex/cli/task-notifier.sh}"
# Standby gating. GRACE_PERIOD: how long "no session-role watcher for
# this inbox" must hold, continuously, before arming. ROLE_POLL: how often
# that's checked, both in standby and while armed, so a session watcher
# appearing later still disarms this one. Generic knobs, not worker-specific:
# whether a given instance runs this supervisor at all, and what these are
# set to, is for whoever launches it (core's own default, or a skill's
# config) to decide -- this script only reads them.
GRACE_PERIOD="${SUTANDO_NOTIFIER_GRACE_PERIOD:-45}"
ROLE_POLL="${SUTANDO_NOTIFIER_ROLE_POLL:-5}"
child_pid=""

# yes / no / unknown -- unknown (ps snapshot unavailable) is deliberately
# never read as "no": every caller below fails toward keeping whatever
# coverage already exists rather than risk a second decider.
# The sentinel dir is the workspace the launcher names, else the inbox's parent:
# the watcher's own fallback, so both read the same file. No inbox, no gate.
session_role_verdict() {
  local args=(role-present session) out rc
  [ -n "$TASKS_DIR" ] && args+=(--inbox "$TASKS_DIR" --ready "${SUTANDO_WORKSPACE_DIR:-$(dirname "$TASKS_DIR")}/state")
  out="$("$PY" "$WATCHER_IDENTITY" "${args[@]}" 2>/dev/null)"
  rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    printf '%s\n' "$out"
  else
    echo "unknown"
  fi
}

stop_child() {
  [ -n "$child_pid" ] || return 0
  # The Python child calls setsid(), so its PID is also the notifier process
  # group's ID. Stop the whole group; fall back to the leader during the tiny
  # pre-setsid race.
  kill -TERM "-$child_pid" 2>/dev/null || kill -TERM "$child_pid" 2>/dev/null || true
  wait "$child_pid" 2>/dev/null || true
  child_pid=""
}

trap 'stop_child; exit 0' HUP INT TERM

# One notifier lifetime while armed. Returns 0 when it crashed and should be
# restarted (caller's armed-loop continues), 1 when the target died (caller's
# armed-loop and the outer standby loop both end), 2 when a session-role
# watcher appeared and this notifier was stopped to yield to it (caller
# returns to standby).
run_notifier_once() {
  # watch-tasks-stream.sh deliberately uses `kill 0` when its fswatch pipeline
  # ends so no orphan child survives. Run the notifier in a separate process
  # group; otherwise that cleanup signal also kills this supervisor and tmux
  # removes the entire watcher session—the production failure fixed here.
  "$PY" -c \
    'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1]])' \
    "$NOTIFIER" &
  child_pid=$!
  # Watch the role verdict while the child runs, UNCONDITIONALLY -- a
  # session-role watcher can arm at any moment and must end this notifier's
  # turn whether or not a pane/window was declared. Target liveness is only
  # checked here when declared (matching the original, pane/window-scoped
  # behavior); undeclared, it's still caught at the outer `while target_alive`
  # once this notifier lifetime ends, same as before this change.
  while kill -0 "$child_pid" 2>/dev/null; do
    if declared_target && ! target_alive; then
      stop_child
      return 1
    fi
    [ "$(session_role_verdict)" = "yes" ] && { stop_child; return 2; }
    sleep "$TARGET_POLL"
  done
  wait "$child_pid"
  local status=$?
  child_pid=""
  target_alive || return 1
  [ "$(session_role_verdict)" = "yes" ] && return 2
  echo "task-notifier-supervisor: notifier exited with status $status; restarting" >&2
  sleep "$RESTART_DELAY"
  return 0
}

# "unknown" keeps whatever runs: while armed, the notifier stays; in standby,
# nothing is running to keep, and a host whose ps never answers would otherwise
# never get a watcher at all -- so an unknown that persists for the whole grace
# period arms, the same wait a clean "no" gets.
unknown_since=""
while target_alive; do
  verdict="$(session_role_verdict)"
  if [ "$verdict" = "yes" ]; then
    unknown_since=""
    sleep "$ROLE_POLL"
    continue
  fi
  if [ "$verdict" = "unknown" ]; then
    now="$(date +%s)"
    [ -n "$unknown_since" ] || unknown_since="$now"
    if [ $((now - unknown_since)) -lt "$GRACE_PERIOD" ]; then
      sleep "$ROLE_POLL"
      continue
    fi
  else
    unknown_since=""
    waited=0
    clear_to_arm=1
    while [ "$waited" -lt "$GRACE_PERIOD" ]; do
      target_alive || { clear_to_arm=0; break; }
      sleep "$ROLE_POLL"
      waited=$((waited + ROLE_POLL))
      v="$(session_role_verdict)"
      if [ "$v" = "yes" ]; then
        clear_to_arm=0
        break
      fi
      # "unknown" mid-wait: keep waiting rather than reset the timer or arm
      # early -- a transient ps failure costs time, never coverage either way.
    done
    [ "$clear_to_arm" -eq 1 ] || continue
  fi
  unknown_since=""
  while target_alive; do
    run_notifier_once
    rc=$?
    [ "$rc" -eq 0 ] || break   # 1: target gone (outer loop ends); 2: disarmed (back to standby)
  done
done

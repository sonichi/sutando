#!/bin/bash
# Keep the Codex task notifier alive for as long as the core tmux session
# lives -- but ONLY while no in-session (--role session) watcher already
# covers this inbox. The two hosting modes (a live session's own Monitor
# watcher, vs this supervisor's external notifier+watcher) are mutually
# exclusive by code now, not by an agent instruction: this script
# starts in standby, arms after a grace period with no session-role watcher
# seen for its inbox, and disarms the moment one appears.
set -u

# BASH_SOURCE, not $0: this file's own path whether executed or sourced (a test
# sources it to exercise the helpers); $0 would be the sourcing shell's name.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
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

# The same pane the notifier types into, resolved the same way, for the pre-arm
# classify: a declared pane, else the session's core window.
NUDGE_TARGET="${SUTANDO_TMUX_PANE:-$SESSION:${SUTANDO_TMUX_WINDOW:-0}}"
STALE_S="${SUTANDO_NOTIFIER_BEAT_STALE_S:-90}"

# classify_pane's verdict for the target, or "unknown" when the pane cannot be
# read or classified -- unknown is never idle, so it never earns a nudge.
pane_state() {
  local cap
  cap="$(tmux -S "$TMUX_SOCKET" capture-pane -p -e -J -t "$NUDGE_TARGET" 2>/dev/null)" || { echo unknown; return; }
  printf '%s' "$cap" | "$PY" -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from delivery.pane_gate import classify_pane, CLAUDE; print(classify_pane(sys.stdin.read(), CLAUDE).state)' \
    "$REPO/src" 2>/dev/null || echo unknown
}

# The liveness beat file for THIS session: a worker's when launched as one
# (SUTANDO_INSTANCE_ID set), else the core's by host label. Empty when the
# workspace or host label cannot be resolved -- the caller then treats health as
# unknown, which arms, rather than as absent, which would suppress the standby.
beat_path_for_session() {
  local ws
  ws="${SUTANDO_WORKSPACE_DIR:-$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)}"
  [ -n "$ws" ] || return 0
  if [ -n "${SUTANDO_INSTANCE_ID:-}" ]; then
    printf '%s/state/workers/%s.alive' "$ws" "$SUTANDO_INSTANCE_ID"
  else
    local host
    host="$(bash "$REPO/scripts/sutando-config.sh" host-label 2>/dev/null)" || return 0
    [ -n "$host" ] || return 0
    printf '%s/state/cores/%s.alive' "$ws" "$host"
  fi
}

# Only a notifier that implements the one-shot --nudge entrypoint can be nudged.
# The shared supervisor's $NOTIFIER defaults to the Codex notifier, which has no
# --nudge handler -- passing it would fall through to that notifier's long-lived
# event loop and never return, wedging nudge_and_wait. A grep for the exact
# entrypoint, never a probe run (a run has the same fall-through hazard).
notifier_supports_nudge() {
  [ -r "$NOTIFIER" ] && grep -q '"--nudge"' "$NOTIFIER" 2>/dev/null
}

# nudge / alert / arm, from the pane verdict and the beat freshness. An
# unresolvable beat path is passed as health=unknown, which decides "arm". A
# notifier without --nudge can only ever arm, so short-circuit there: this also
# avoids classifying a non-Claude pane with the Claude profile pane_state uses.
decide_action() {
  notifier_supports_nudge || { echo arm; return; }
  local ps beat
  ps="$(pane_state)"
  beat="$(beat_path_for_session)"
  if [ -n "$beat" ]; then
    "$PY" "$REPO/src/delivery/nudge_gate.py" --pane-state "$ps" --beat-path "$beat" --stale-s "$STALE_S" 2>/dev/null || echo arm
  else
    "$PY" "$REPO/src/delivery/nudge_gate.py" --pane-state "$ps" --health unknown 2>/dev/null || echo arm
  fi
}

# One nudge, then wait grace-until-idle for the session to re-arm its own
# watcher. Returns 0 when a session-role watcher appears (restored -- the caller
# stays in standby), 1 when the nudge could not be delivered or none appeared
# within the grace (the caller arms the standby instead).
nudge_and_wait() {
  "$PY" -c 'import os, sys; os.setsid(); os.execv("/bin/bash", ["bash", sys.argv[1], "--nudge"])' "$NOTIFIER" \
    || { echo "task-notifier-supervisor: nudge could not be delivered; arming" >&2; return 1; }
  local waited=0
  while [ "$waited" -lt "$GRACE_PERIOD" ]; do
    target_alive || return 1
    [ "$(session_role_verdict)" = "yes" ] && {
      echo "task-notifier-supervisor: nudge restored the session watcher; staying in standby" >&2
      return 0
    }
    sleep "$ROLE_POLL"
    waited=$((waited + ROLE_POLL))
  done
  echo "task-notifier-supervisor: nudge did not restore a session watcher within the grace; arming" >&2
  return 1
}

# An idle frame over a dead or hung agent: surface it once for a restart (the
# owner's lane). The caller still arms afterwards -- see the alert branch.
health_alert() {
  echo "task-notifier-supervisor: pane idle-ready but the session beat is stale/absent for $NUDGE_TARGET; needs a restart (arming the standby meanwhile)" >&2
  command -v osascript >/dev/null 2>&1 \
    && osascript -e 'display notification "A Sutando session looks idle but its heartbeat is stale. It may need a restart." with title "Sutando"' >/dev/null 2>&1 || true
}
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
# One-shot latch for the idle-but-dead health alert: raised when it fires, so a
# persistent stale/absent beat does not re-notify every grace period; lowered
# again the moment the decision is anything but alert (the session recovered).
alerted=0

# yes / no / unknown -- unknown (ps snapshot unavailable) is deliberately
# never read as "no": every caller below fails toward keeping whatever
# coverage already exists rather than risk a second decider.
# The sentinel dir is derived exactly as the watcher derives it (one helper in
# tasks-dir-resolve.sh), so both name the same file. No inbox, no gate.
session_role_verdict() {
  local args=(role-present session) out rc
  [ -n "$TASKS_DIR" ] && args+=(--inbox "$TASKS_DIR" --ready "$(workspace_dir_for_inbox "$TASKS_DIR")/state")
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

# Sourced by a test to exercise the helpers in isolation: stop here, before the
# standby loop, so decide_action and its callees can be invoked directly.
if [ "${SUTANDO_SUPERVISOR_SOURCE_ONLY:-}" = "1" ]; then return 0 2>/dev/null || exit 0; fi

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
    # Grace elapsed on a clean "no session watcher". Before arming the external
    # standby, prefer restoring the session's own watcher when it is idle and
    # alive; the unknown-verdict path above skips this and arms as before.
    case "$(decide_action)" in
      nudge)
        alerted=0
        nudge_and_wait && continue   # restored -> back to standby, do not arm
        ;;                            # not restored -> fall through and arm
      alert)
        # Idle frame over a dead/hung agent. Surface it ONCE (a restart is the
        # owner's lane), then still ARM: arming is the current behaviour, it
        # keeps coverage for when the session recovers, and the notifier's own
        # paste gate refuses to inject into an unhealthy pane anyway. Not
        # arming here looped this branch, re-alerting every grace period.
        [ "$alerted" = "1" ] || { health_alert; alerted=1; }
        ;;                            # fall through to arm
      *)
        alerted=0                     # arm (or any other verdict): reset the latch
        ;;
    esac
  fi
  unknown_since=""
  while target_alive; do
    run_notifier_once
    rc=$?
    [ "$rc" -eq 0 ] || break   # 1: target gone (outer loop ends); 2: disarmed (back to standby)
  done
done

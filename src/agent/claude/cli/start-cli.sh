#!/bin/bash
# src/agent/claude/cli/start-cli.sh — canonical launch script for the sutando-core
# tmux session. Single source of truth for the "how to start Claude Code" command,
# so startup.sh + Sutando.app's Restart Core menu can both invoke it without
# duplicating the launch arguments. Core-only: a pool worker's own launch is a
# separate script that sources the same session-launch.sh mechanics this file
# does but owns none of the core-specific ceremony below (restart, notifier,
# monitor, shutdown sentinel, attach/heal). Neither script names the other.
#
# Usage:
#   bash src/agent/claude/cli/start-cli.sh           # start (or attach if running)
#   bash src/agent/claude/cli/start-cli.sh --restart # kill existing session then start fresh
#   ... --restart --visible / --visible               # additionally open a Terminal window attached (macOS)
#
# Per Chi's prompt 2026-05-05 ("shall we add core CLI-related commands in
# sutando app"): extracting the launch command from startup.sh's inline tmux
# block lets the menu-bar app's Restart Core action invoke the same canonical
# entry without re-implementing the tmux flags.

set -e

# This script lives at src/agent/claude/cli/ — four levels under the repo root.
# Pure bash, no external dirname: this is the launcher's own first line, run
# before anything has confirmed PATH resolves basic commands at all.
case "$0" in
  */*) _self_dir="${0%/*}" ;;
  *)   _self_dir="." ;;
esac
REPO="$(cd "$_self_dir/../../../.." && pwd)"
unset _self_dir
cd "$REPO"
# Shared with the codex launcher: one owner for the in-session restart policy.
. "$REPO/src/agent/restart-guard.sh"
# Named check before sourcing: an unexplained "No such file or directory" from
# a bare `.` here reads as a broken launcher, not a fixture/checkout missing
# this file's own sibling — a real trap for a scratch-repo test fixture.
if [ ! -r "$REPO/src/agent/claude/cli/session-launch.sh" ]; then
  echo "start-cli.sh: missing its sibling src/agent/claude/cli/session-launch.sh — refusing to start (a scratch checkout/fixture must copy it alongside this file)" >&2
  exit 1
fi
# shellcheck source=session-launch.sh
. "$REPO/src/agent/claude/cli/session-launch.sh"

resolve_claude_py
# shellcheck source=skill-manifest-config.sh
[ -r "$REPO/src/skill-manifest-config.sh" ] && . "$REPO/src/skill-manifest-config.sh"

# Honor a caller-provided socket (e.g. a desktop app that runs a user-private tmux
# runtime under its app-support dir); default to the shared /tmp socket for dev/CLI.
# Backward-compatible: unset → identical to the previous hardcoded value.
TMUX_SOCKET="${SUTANDO_TMUX_SOCKET:-/tmp/sutando-tmux.sock}"

# A tmux server inherits its GLOBAL environment from whichever process starts it,
# and `start-server` on a serverless socket is a no-op — so unset before any tmux.
unset SUTANDO_CORE_MODEL
SESSION="${SUTANDO_TMUX_SESSION:-sutando-core}"
# The external task notifier runs beside the core in its own tmux session,
# under the runtime-agnostic supervisor (parameterised by SUTANDO_NOTIFIER_SCRIPT).
WATCHER_SESSION="${SESSION}-watcher"
NOTIFIER_SUPERVISOR="$REPO/src/agent/codex/cli/task-notifier-supervisor.sh"
NOTIFIER_SCRIPT="$REPO/src/agent/claude/cli/task-notifier.sh"
SURFACE_ARGS=(--remote-control "Sutando" --chrome)
# `/startup` is the CANONICAL CORE's ceremony: orphan recovery, session crons,
# a gate any watcher satisfies. One arg — the skill reads it as $ARGUMENTS.
BOOT_PROMPT="/startup"
SESSION_ARGS=()
if [ -n "${SUTANDO_CLAUDE_RESUME:-}" ]; then
  SESSION_ARGS=(--resume "$SUTANDO_CLAUDE_RESUME")
elif [ -n "${SUTANDO_CLAUDE_SESSION_ID:-}" ]; then
  SESSION_ARGS=(--session-id "$SUTANDO_CLAUDE_SESSION_ID")
fi

# Marker identifying THIS process as the long-lived sutando-core session (as
# opposed to an ad-hoc `claude` in the same checkout — PR review, codex, etc.).
# The SessionStart hook (src/schedule-crons-session-hint.sh) gates its
# /startup bootstrap reminder on this so only the core triggers cron
# registration, never every session in the checkout. Exported so the no-tmux
# `exec claude` fallback inherits it directly; injected into the tmux launch
# branches via `new-session -e` (below) since tmux runs the command under the
# server's environment, not necessarily this shell's.
# Snapshot what we INHERITED before the export below overwrites it: the
# in-session restart guard must not read the marker this script sets itself.
CALLER_CORE_SESSION="${SUTANDO_CORE_SESSION:-}"
export SUTANDO_CORE_SESSION=1

# Called ONLY from paths that create or heal a core. Attaching to a live one
# must not clear: that would cancel a `--stop-only` still waiting to be observed.
clear_shutdown_sentinel() {
  if [ -n "$PY" ]; then
    "$PY" "$REPO/src/shutdown.py" clear >/dev/null \
      || echo "start-cli.sh: shutdown.py clear failed — the intake gate may hold tasks" >&2
  else
    echo "start-cli.sh: no runnable interpreter — shutdown sentinel NOT cleared" >&2
  fi
}

# Clearing the sentinel before a launch that never yields a live core opens
# intake with nothing serving, so exec paths stash it and restore on failure.
_SENTINEL_STASH=""
# Without execfail bash exits 127 on a failed exec, which would make every
# restore-after-exec below unreachable dead code.
shopt -s execfail
stash_shutdown_sentinel() {
  _SENTINEL_STASH=""
  [ -n "$PY" ] || return 0
  _sp="$("$PY" "$REPO/src/shutdown.py" path 2>/dev/null)" || return 0
  [ -n "$_sp" ] && [ -f "$_sp" ] || return 0
  _SENTINEL_STASH="$(mktemp "${TMPDIR:-/tmp}/sutando-sentinel.XXXXXX" 2>/dev/null)" || return 0
  cp "$_sp" "$_SENTINEL_STASH" 2>/dev/null || _SENTINEL_STASH=""
}
# Only reachable when exec FAILED: exec never returns on success.
restore_shutdown_sentinel() {
  [ -n "$_SENTINEL_STASH" ] && [ -f "$_SENTINEL_STASH" ] || return 0
  _sp="$("$PY" "$REPO/src/shutdown.py" path 2>/dev/null)" || return 0
  if [ -n "$_sp" ]; then
    mkdir -p "$(dirname "$_sp")" 2>/dev/null || true
    cp "$_SENTINEL_STASH" "$_sp" 2>/dev/null \
      || echo "start-cli.sh: could not restore the shutdown sentinel after a failed launch" >&2
  fi
  rm -f "$_SENTINEL_STASH" 2>/dev/null || true
  _SENTINEL_STASH=""
}
export SUTANDO_CORE_RUNTIME=claude
ENV_ARGS=(-e SUTANDO_CORE_RUNTIME=claude -e SUTANDO_CORE_SESSION=1)
[ -n "${SUTANDO_TMUX_SOCKET:-}" ] && ENV_ARGS+=(-e "SUTANDO_TMUX_SOCKET=$SUTANDO_TMUX_SOCKET")
[ -n "${SUTANDO_TMUX_SESSION:-}" ] && ENV_ARGS+=(-e "SUTANDO_TMUX_SESSION=$SUTANDO_TMUX_SESSION")
# Forward the embedder-provided default workspace into the core session for the
# SAME reason as above (tmux takes the server env, not this shell's). Without
# this the core's own resolve_workspace() (proactive-loop, task scripts) misses
# $SUTANDO_DEFAULT_WORKSPACE and falls back to {repo}/workspace — while the
# gateway window (which gets it explicitly) resolves to that path: a split-brain
# where the two watch different tasks/ dirs. Companion to the resolver change
# (#2094); conditional so non-bundled/OSS installs are untouched.
[ -n "${SUTANDO_DEFAULT_WORKSPACE:-}" ] && ENV_ARGS+=(-e "SUTANDO_DEFAULT_WORKSPACE=$SUTANDO_DEFAULT_WORKSPACE")
# Product deployments can disable autonomous repo development while keeping
# owner tasks, health checks, and the task watcher active. Explicitly forward
# the override because tmux may use an older server environment.
if [ "${SUTANDO_SELF_DEVELOPMENT_ENABLED+x}" = x ]; then
  ENV_ARGS+=(-e "SUTANDO_SELF_DEVELOPMENT_ENABLED=$SUTANDO_SELF_DEVELOPMENT_ENABLED")
fi
forward_skill_manifest_config
ENV_ARGS+=(${SKILL_MANIFEST_ENV_ARGS[@]+"${SKILL_MANIFEST_ENV_ARGS[@]}"})
resolve_claude_credential_proxy
if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
  ENV_ARGS+=(-e "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL")
fi
# Test probe: dump the assembled core env forwarding and exit — lets the
# regression suite assert the proxy-routing policy (live listener forwards,
# dead port omits, caller preset wins) against the REAL ENV_ARGS under
# a stubbed lsof, without touching tmux. No production caller passes this.
if [ "${1:-}" = "--print-core-env" ]; then
  printf '%s\n' ${ENV_ARGS[@]+"${ENV_ARGS[@]}"}
  exit 0
fi

# Registers the PERSONAL_CLAUDE.md compaction-reinject hook. Below the probe
# exit: --print-core-env is a pure read and must not write settings.
install_claude_personal_hook

# Optional working-directory override for the core `claude` process.
#   - Unset (upstream default): no override — the core launches from $REPO (the
#     script's cwd), exactly as before. Zero behavior change for OSS installs.
#   - Set (e.g. Sutando.app exports SUTANDO_CLAUDE_WORKING_DIR=$HOME/.sutando/repo):
#     anchor the core's CWD there instead — see resolve_claude_cwd_args for why.
resolve_claude_cwd_args

# Resolve workspace-scoped CLAUDE_CONFIG_DIR and seed onboarding/trust/bypass
# state — see session-launch.sh for the full rationale. This is the single
# launch chokepoint (Sutando.app's launchCore, the terminal-server Core CLI
# pane, and src/startup.sh all exec this script), so seeding here covers
# every core path.
resolve_claude_config_dir_and_seed

# NO --model flag: the core inherits the user's global model, so 1M stays the
# default. An ambient env pin is indistinguishable from a deliberate choice.
resolve_claude_settings_args
apply_claude_obs_metering

# --restart: kill any existing session before starting fresh. Without this,
# the script's "already running → attach" path returns and the old session
# keeps running.
#
# HAZARD: --restart MUST NOT be invoked from inside the sutando-core
# session itself — kill-session terminates the running agent mid-task.
# Safe callers: Sutando.app menu, terminal one-off, future health-check
# emit-task. Unsafe: a future agent processing a "restart core" task by
# exec'ing this script from within sutando-core. Per Mini's #608 review.
# --visible (sonichi#2410): after boot (or on an already-running no-TTY
# re-run), open a Terminal window attached to the core session via a generated
# .command file + `open -a Terminal` — the TCC-free GUI-exec path proven in the
# 2026-07-29 recovery (no AppleEvents, no Automation permission, creates the
# window when none exists). No-op off macOS / without `open` / headless-only
# callers that don't pass it.
VISIBLE=0
for _arg in "$@"; do
  [ "$_arg" = "--visible" ] && VISIBLE=1
done

open_visible_terminal() {
  [ "$(uname)" = "Darwin" ] || return 0
  command -v open > /dev/null 2>&1 || return 0
  local ws cmdfile
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mkdir -p "$ws/state"
  cmdfile="$ws/state/attach-core.command"
  {
    echo '#!/bin/bash'
    echo '# Auto-generated by start-cli.sh --visible (sonichi#2410) — attaches this'
    echo '# Terminal window to the core session. Safe to re-run; delete freely.'
    echo "exec tmux -S '$TMUX_SOCKET' attach -t '$SESSION'"
  } > "$cmdfile"
  chmod +x "$cmdfile"
  open -a Terminal "$cmdfile" 2>/dev/null || true
}

# Two modes, per sonichi's "force restart should be separate from restart":
#   --restart       graceful: SIGTERM + wait; if the core won't stop cleanly it
#                   ABORTS (it may be mid-task) — never SIGKILLs on its own.
#   --force-restart SIGTERM → SIGKILL escalation for a wedged/unresponsive core.
# Both then fall through to the create path, which verifies the new core is live
# before exit 0 (no silent false-success). Every attempt is logged (timestamped)
# to logs/restart-attempts.log so a failed restart is diagnosable even when the
# caller (Sutando.app) routes our stdout to /dev/null — the gap that made the
# 2026-07-30 outage invisible.
RESTART_REQUESTED=""
FORCE_RESTART=""
case "${1:-}" in
  --restart)       RESTART_REQUESTED=1 ;;
  --force-restart) RESTART_REQUESTED=1; FORCE_RESTART=1 ;;
esac
# graceful-restart.sh exec's this script holding its lock; an abort here would
# otherwise leave that lock to age out (15 min) and defer the owner's next click.
release_orchestrator_lock() {
  [ -n "${GR_RID:-}" ] || return 0
  local ws; ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  local lock="$ws/state/locks/graceful-restart.lock"
  [ "$(cat "$lock/rid" 2>/dev/null)" = "$GR_RID" ] && rm -rf "$lock"
  return 0
}
log_restart_attempt() {
  local ws; ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mkdir -p "$ws/logs" 2>/dev/null || true
  printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "${FORCE_RESTART:+force-restart}${FORCE_RESTART:-restart}" "$1" \
    >> "$ws/logs/restart-attempts.log" 2>/dev/null || true
}
# Enforces the HAZARD above; the decision and its message are shared with
# the codex launcher, this adapter keeps only its own attempt logging.
if [ -n "$RESTART_REQUESTED" ] && sutando_restart_guard_refuses "$CALLER_CORE_SESSION"; then
  sutando_restart_guard_explain
  log_restart_attempt "$SUTANDO_RESTART_GUARD_REASON"
  exit 1
fi
if [ -n "$RESTART_REQUESTED" ]; then
  log_restart_attempt "begin (session=$(claude_named_tmux_session_exists && echo up || echo none) core=$(claude_named_process_running && echo up || echo none))"
  if claude_named_tmux_session_exists || claude_named_process_running; then
    echo "Killing existing $SESSION session..."
    tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
    tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
    claude_named_pids | while read -r pid; do
      [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    # The kill is already issued, so this wait can only be bounded, never
    # abandoned: a SessionEnd handoff takes longer than a few seconds.
    GRACE_S="${SUTANDO_RESTART_GRACE_S:-90}"
    _ticks=$(( GRACE_S * 5 ))
    while [ "$_ticks" -gt 0 ] && { claude_named_tmux_session_exists || claude_named_process_running; }; do
      sleep 0.2; _ticks=$(( _ticks - 1 ))
    done
    if claude_named_tmux_session_exists || claude_named_process_running; then
      if [ -n "$FORCE_RESTART" ]; then
        # force-restart: the core is wedged; escalate to SIGKILL, then poll ~3s.
        echo "  core still alive ${GRACE_S}s after SIGTERM — force-restart escalating to SIGKILL" >&2
        tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
        claude_named_pids | while read -r pid; do
          [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done
        for _ in $(seq 1 15); do
          claude_named_tmux_session_exists || claude_named_process_running || break
          sleep 0.2
        done
      else
        # plain restart must NOT hammer: the core may be legitimately mid-task.
        # Abort loud and point at force-restart rather than risk killing work.
        echo "  ⚠ $SESSION core did not stop within ${GRACE_S}s of SIGTERM." >&2
        echo "    'restart' won't SIGKILL a wedged core — re-run as: bash $0 --force-restart" >&2
        log_restart_attempt "abort: core would not stop within ${GRACE_S}s (needs --force-restart)"
        release_orchestrator_lock
        exit 1
      fi
    fi
    # After force escalation, still alive → hard abort rather than stack a second
    # core on a survivor (double task-consumer) or exit-0 a half-torn-down state.
    if claude_named_tmux_session_exists || claude_named_process_running; then
      echo "  ⚠ $SESSION core did not die after SIGKILL — aborting force-restart." >&2
      echo "    Investigate the stuck pid; rerun once it's gone." >&2
      log_restart_attempt "abort: core survived SIGKILL"
      release_orchestrator_lock
      exit 1
    fi
  fi
  log_restart_attempt "kill-complete; creating fresh core"
fi

# Agent Shepherd M1 monitor (PR #2100). Watch the CANONICAL sutando-core session
# for blocked-on-input gates the no-TTY core can't answer (/login, a mid-session
# permission prompt, an unknown dialog) and write state/core-supervisor.json —
# consumed by the desktop "Action needed" banner and the communicator relay.
# Launched HERE, the one place that knows the canonical TMUX_SOCKET + SESSION —
# NOT from startup.sh, whose $TMUX is empty in the Sutando.app/background path
# (so a $TMUX-derived wiring would never start the monitor for the real core).
# The guard is scoped to THIS socket so a monitor for a different core/socket
# can never suppress this one. Socket only, not socket + out path: the desktop
# launcher (launch-sutando.sh) starts the same watcher inside tmux with its own
# workspace spelling for --out, and matching on that path let both run.
watcher_session_exists() {
  tmux -S "$TMUX_SOCKET" has-session -t "=$WATCHER_SESSION" 2>/dev/null
}

# The version fingerprint says the session was CORRECTLY configured at some
# point; it says nothing about whether watch-tasks-stream.sh is still running
# right now. task-notifier-supervisor.sh restarts a crashed notifier on its
# own, but only while its own process is alive -- a hung supervisor, or a gap
# inside its RESTART_DELAY, leaves the session looking healthy (right version,
# session present) while no watcher process actually exists. Without this
# check, ensure_task_notifier's version-match shortcut is a no-op for exactly
# the condition its caller (checkWatcher) exists to repair (#4451 review).
#
# Scoped to CORE's own sentinel PID file (util_paths.py watcher-sentinel), the
# same mechanism watch-tasks-stream.sh's own PID_FILE uses -- a bare `pgrep -f
# watch-tasks-stream.sh` matches EVERY worker's watcher on a shared host too,
# which is exactly the unscoped-substring failure mode this repo already paid
# for once tonight (an unscoped pkill collaterally killed other sessions'
# production watchers). One instance's liveness must never be answered by
# another instance's process.
# The session is healthy while its supervisor runs: in standby it has no
# notifier or watcher child by design, so a sentinel-pid test would read a
# correctly idle session as dead and replace it on every rerun.
watcher_process_alive() {
  local pane_pid
  pane_pid="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$WATCHER_SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
  [ -n "$pane_pid" ] && [ "$pane_pid" -eq "$pane_pid" ] 2>/dev/null && kill -0 "$pane_pid" 2>/dev/null
}

# The live core's own window and pane, read from the pane that runs it: a heal
# may have placed it off index 0, and a plain rerun must not forget that.
resolve_core_target() {
  local pid row
  CORE_PANE=""
  for pid in $(claude_named_pids); do
    row="$(tmux -S "$TMUX_SOCKET" list-panes -s -t "=$SESSION" -F '#{window_index} #{pane_id} #{pane_pid}' 2>/dev/null \
      | awk -v p="$pid" '$3 == p {print $1, $2; exit}')"
    if [ -n "$row" ]; then
      CORE_WINDOW="${row%% *}"
      CORE_PANE="${row##* }"
      return 0
    fi
  done
  CORE_WINDOW="${CORE_WINDOW:-0}"
}

# Standby delivery path: pastes a queued task into the core pane only when the
# pane is idle-ready and no result exists, so self-arm via Monitor stays primary.
# Core-only: a pool worker never runs a standby/supervisor pairing (#4477/#4585).
ensure_task_notifier() {
  local expected_version active_version version_files notifier_py
  resolve_core_target
  # The launcher-resolved interpreter or nothing: a bare PATH python3 on a Mac
  # without the developer tools is the CLT stub, and the supervisor would run it every second.
  if [ -z "$PY" ]; then
    echo "  ⚠ task notifier not started: no runnable Python interpreter (scripts/python-binary.sh); the health probe will report it missing" >&2
    return 0
  fi
  notifier_py="$PY"
  version_files=(
    "$NOTIFIER_SUPERVISOR"
    "$NOTIFIER_SCRIPT"
    "$REPO/src/core-input-watch.py"
    "$REPO/src/delivery/task_dispatch.py"
    "$REPO/src/tasks-dir-resolve.sh"
    "$REPO/src/watcher_identity.py"
  )
  # No resolution here: the watcher reads <workspace>/state/task-event-handler.json
  # itself and fswatches it for changes, so the launcher forwards only a genuine
  # operator pin (if one is already set) and nothing computed.
  # The target window is part of the identity: a heal that lands the core on a
  # new index must replace a watcher still aimed at the old one.
  expected_version="$(cksum "${version_files[@]}" | cksum | awk '{print $1 "-" $2}')-w${CORE_WINDOW:-0}-p${CORE_PANE:-none}-h$(printf '%s' "${SUTANDO_TASK_EVENT_HANDLER:-}" | cksum | awk '{print $1}')-y$(printf '%s' "$notifier_py" | cksum | awk '{print $1}')"
  if watcher_session_exists; then
    active_version="$(
      tmux -S "$TMUX_SOCKET" show-environment -t "=$WATCHER_SESSION" \
        SUTANDO_NOTIFIER_VERSION 2>/dev/null \
        | sed -n 's/^SUTANDO_NOTIFIER_VERSION=//p' || true
    )"
    # A version match alone means the session was once configured correctly,
    # not that its watcher process is running now -- confirm liveness too, or
    # this shortcut is a no-op for the exact absence it exists to repair.
    if [ "$active_version" = "$expected_version" ] && watcher_process_alive; then
      return 0
    fi
    tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
  fi
  NOTIFIER_ENV_ARGS=(-e "SUTANDO_TMUX_SOCKET=$TMUX_SOCKET" -e "SUTANDO_TMUX_SESSION=$SESSION")
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_NOTIFIER_SCRIPT=$NOTIFIER_SCRIPT")
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_NOTIFIER_VERSION=$expected_version")
  [ -n "${SUTANDO_TASKS_DIR:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TASKS_DIR=$SUTANDO_TASKS_DIR")
  [ -n "${SUTANDO_RESULTS_DIR:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_RESULTS_DIR=$SUTANDO_RESULTS_DIR")
  [ -n "${SUTANDO_WORKSPACE_DIR:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_WORKSPACE_DIR=$SUTANDO_WORKSPACE_DIR")
  # Standby/grace-period knobs: unset here means the supervisor keeps
  # its own generic defaults. A skill that needs different pacing for an
  # instance it spawns sets these in ITS environment before this launcher
  # runs, same forwarding pattern as every other var above.
  [ -n "${SUTANDO_NOTIFIER_GRACE_PERIOD:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_NOTIFIER_GRACE_PERIOD=$SUTANDO_NOTIFIER_GRACE_PERIOD")
  [ -n "${SUTANDO_NOTIFIER_ROLE_POLL:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_NOTIFIER_ROLE_POLL=$SUTANDO_NOTIFIER_ROLE_POLL")
  # A required Team handler must reach the watcher, or its refusal (rc 4) is never seen.
  [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TASK_EVENT_HANDLER=$SUTANDO_TASK_EVENT_HANDLER")
  # The exact core window: a heal may land the core off index 0 beside a sibling.
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TMUX_WINDOW=${CORE_WINDOW:-0}")
  [ -n "$CORE_PANE" ] && NOTIFIER_ENV_ARGS+=(-e "SUTANDO_TMUX_PANE=$CORE_PANE")
  # The launcher-resolved interpreter, never a bare name from the watcher's PATH.
  NOTIFIER_ENV_ARGS+=(-e "SUTANDO_NOTIFIER_PY=$notifier_py")
  tmux -S "$TMUX_SOCKET" new-session -d -s "$WATCHER_SESSION" \
    "${NOTIFIER_ENV_ARGS[@]}" bash "$NOTIFIER_SUPERVISOR"
}

# Core-only: a pool worker is never the subject of the Agent Shepherd monitor.
ensure_core_monitor() {
  local ws mon_out relay_pid_file relay_state
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  mon_out="$ws/state/core-supervisor.json"
  # Monitor (PR #2100): launch unless one for this exact socket+out is running.
  if [ -n "$PY" ] && ! pgrep -f "core-input-watch\.py .*--socket ${TMUX_SOCKET}( |$)" > /dev/null 2>&1; then
    "$PY" "$REPO/src/core-input-watch.py" \
      --socket "$TMUX_SOCKET" --session "$SESSION" --out "$mon_out" \
      > /tmp/core-input-watch.log 2>&1 &
  fi
  # Communicator relay (PR #2101): the monitor only WRITES core-supervisor.json;
  # nothing escalated it, so a hard-blocker (blocked-human / logged-out) on a
  # no-TTY core never reached an away owner. Run the one-shot relay on a cadence
  # so those states escalate to the owner's most-recently-active channel
  # (--active-from). The relay debounces internally (--state-file), so re-running
  # every 30s escalates a given stuck episode exactly once. Pidfile + kill -0
  # guard so an attach/re-run never double-starts the loop. Only blocked-human /
  # logged-out escalate (relay's HARD_ESCALATE); hung/crashed are RECOVER's job.
  relay_pid_file="$ws/state/core-supervisor-relay-loop.pid"
  relay_state="$ws/state/core-supervisor-relay.state"
  if ! { [ -f "$relay_pid_file" ] && kill -0 "$(cat "$relay_pid_file" 2>/dev/null)" 2>/dev/null; }; then
    # Redirect the WHOLE subshell (not just the inner python) to the log, so the
    # backgrounded loop does not inherit/hold this script's stdout/stderr — else a
    # caller that captures start-cli.sh's output (e.g. tests/start-cli-*.test.py)
    # blocks on the pipe until this infinite loop closes it (never) and times out.
    # Mirrors the monitor launch above, which redirects to /tmp/core-input-watch.log.
    # An if-block with a simple command, not `A && ( … ) &`: the `&` on an
    # AND-list forks a wrapper subshell that keeps the caller's stdout/stderr
    # (only the inner subshell gets the redirect) and waits on this infinite
    # loop forever — so any caller capturing start-cli.sh's output never saw
    # EOF (desktop core_restart: 120 s timeout on every fresh boot). This form
    # also makes $! the loop's own pid, so the pidfile can actually stop it.
    # Loop only while the interpreter and script exist, and stop if sleep fails:
    # with them gone (engine dir removed) `while true` would spin at full CPU.
    # And stop once the core session this loop relays for has been gone for three
    # checks: a scratch launch (a test, a PR witness) otherwise leaves its loop
    # running for good, one per launch, since the pidfile is per workspace.
    if [ -n "$PY" ]; then
      bash -c 'miss=0; while command -v "$1" > /dev/null 2>&1 && [ -f "$2" ]; do
        if [ -n "$6" ] && command -v tmux > /dev/null 2>&1; then
          if tmux -S "$6" has-session -t "=$7" 2> /dev/null; then miss=0; else miss=$((miss + 1)); [ "$miss" -lt 3 ] || exit 0; fi
        fi
        "$1" "$2" --signal "$3" --state-file "$4" --active-from "$5"; sleep 30 || exit 1; done' \
        relay-loop "$PY" "$REPO/src/core-supervisor-relay.py" "$mon_out" "$relay_state" "$ws/state/last-owner-activity.json" "$TMUX_SOCKET" "$SESSION" \
        >> /tmp/core-supervisor-relay.log 2>&1 < /dev/null &
      echo $! > "$relay_pid_file"
    fi
  fi
}

# Already running — attach if interactive, else exit cleanly. A managed core is
# live when the tmux session exists AND a `claude --name sutando-core` process
# runs under it. Re-running the script is idempotent: we attach/no-op instead
# of spawning a second core (→ duplicate task consumers).
if claude_named_session_running; then
  apply_claude_tmux_defaults
  ensure_core_monitor   # re-ensure the supervisor monitor on every attach/re-run
  ensure_task_notifier
  if [ -t 1 ] && command -v tmux > /dev/null 2>&1; then
    echo "Attaching to existing $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  if [ "$VISIBLE" = 1 ]; then
    open_visible_terminal
    echo "$SESSION already running — opened a Terminal window attached to it."
    exit 0
  fi
  echo "$SESSION already running."
  echo "To attach: tmux -S $TMUX_SOCKET attach -t $SESSION"
  exit 0
fi

# Orphaned core process, no tmux session — a `claude --name sutando-core` is
# running detached from any managed session (e.g. its tmux server died but the
# child claude survived). Adopt it: do NOT start a second core, which would
# double the task-consumer count. On a non-restart start we reuse the existing
# process; the operator can `--restart` to cleanly recycle it.
#
# Under --restart, do NOT adopt an orphan: we just tore the core down, so a
# claude seen here is either still dying (the SIGKILL escalation above should
# have reaped it) or one a competing launcher spawned in the race window.
# Reusing it would defeat the restart and re-introduce the false-success path.
if [ -z "$RESTART_REQUESTED" ] && claude_named_process_running; then
  echo "$SESSION claude process already running (no tmux session) — reusing it." >&2
  echo "To recycle it cleanly: bash $0 --restart"
  exit 0
fi

if claude_named_tmux_session_exists; then
  # Session alive but the core claude is gone. The old behavior here was
  # kill-session — but the desktop runtime keeps SIBLING windows in this
  # session (gateway, monitor; launch-sutando.sh), so nuking the session tore
  # those down with the dead core: the G10 all-or-nothing gap. Heal WINDOW-
  # SCOPED instead: recreate the core as a new window in the surviving session,
  # with the same env/cwd/flags as the create path below. Target index 0 (the
  # core's conventional home, freed when its process died); fall back to any
  # free index if 0 is somehow occupied. This also makes sutando-ctl.sh's
  # restart-core (kill core window → rerun this script) truly window-scoped.
  echo "  ⚠ $SESSION exists but core Claude is gone — healing core window (sibling windows preserved)" >&2
  apply_claude_tmux_defaults
  CORE_CMD=(claude --name "$SESSION" ${SURFACE_ARGS[@]+"${SURFACE_ARGS[@]}"} --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} ${SESSION_ARGS[@]+"${SESSION_ARGS[@]}"} -- "$BOOT_PROMPT")
  # -P -F prints the index the window ACTUALLY landed on: when index 0 is
  # occupied (e.g. a sibling drifted there) the fallback creates the core at a
  # nonzero index, and selecting a hardcoded :0 would activate the WRONG window
  # (review-caught: attach/Console then shows the gateway, not the healed core).
  healed_idx="$(tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_index}' -t "$SESSION:0" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} "${CORE_CMD[@]}" 2>/dev/null \
    || tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_index}' -t "$SESSION" ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} ${CWD_ARGS[@]+"${CWD_ARGS[@]}"} "${CORE_CMD[@]}")" \
    || healed_idx=""
  if [ -z "$healed_idx" ]; then
    # No window at all: a watcher left from the dead core would type into a sibling.
    echo "  ⚠ could not create a core window in $SESSION — no core is serving." >&2
    tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
    exit 66
  fi
  # Make the healed core the active window so attach/Console show it, not the
  # quiet gateway (same reason launch-sutando.sh creates siblings with -d).
  tmux -S "$TMUX_SOCKET" select-window -t "$SESSION:$healed_idx" 2>/dev/null || true
  ensure_core_monitor
  # new-window returning an index proves tmux ACCEPTED the command, not that the
  # child lives; poll before opening intake, same bound as the fresh-start path.
  for _ in $(seq 1 25); do
    claude_named_session_running && break
    sleep 0.2
  done
  if claude_named_session_running; then
    clear_shutdown_sentinel
    CORE_WINDOW="$healed_idx"
    ensure_task_notifier
  else
    echo "  ⚠ healed window did not come up within ~5s — sentinel NOT cleared, no core is serving." >&2
    # A surviving sibling window keeps the session alive; a watcher would type into it.
    tmux -S "$TMUX_SOCKET" kill-session -t "=$WATCHER_SESSION" 2>/dev/null || true
  fi
  if [ -t 1 ]; then
    echo "Attaching to healed $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "Healed core window in $SESSION (session + sibling windows preserved)."
  exit 0
fi

# Past every attach/adopt/heal exit above, so this is a genuine fresh boot. The
# sentinel is cleared at each launch site below, never before one can fail.

# Auto-install tmux via Homebrew if missing. Sutando.app's
# watcher-auto-restart depends on a tmux-wrapped CLI pane.
if ! command -v tmux > /dev/null 2>&1 && command -v brew > /dev/null 2>&1; then
  echo "tmux not found — installing via Homebrew (~30s, required for Sutando.app watcher-auto-restart)..."
  brew install tmux 2>&1 | tail -3
fi

# Stamp the core session start into an append-only per-boot log. One JSONL
# line per launch; consecutive entries bound each session's lifetime, which
# is what session-recap tooling needs to pick the right transcript (owner
# ask 2026-07-13). Best-effort: never block the launch on it.
if _ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" && [ -n "$_ws" ]; then
  mkdir -p "$_ws/state" 2>/dev/null || true
  python3 "$REPO/src/core_metadata.py" "$_ws" claude "$SESSION" 2>/dev/null || true
  printf '{"host":"%s","session_started_at":%s,"iso":"%s","source":"start-cli"}\n' \
    "$(hostname | sed 's/\..*//')" "$(date +%s)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >> "$_ws/state/session-starts.log" 2>/dev/null || true
fi

# Fall back to a bare `exec claude` if tmux is still missing.
if ! command -v tmux > /dev/null 2>&1; then
  echo "  ⚠ tmux not found — running without tmux wrapper"
  echo "    (Sutando.app's watcher-auto-restart won't work; brew install tmux to enable)"
  [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ] && cd "$SUTANDO_CLAUDE_WORKING_DIR"
  if ! command -v claude >/dev/null 2>&1; then
    echo "  ⚠ claude not found — not clearing the shutdown sentinel, no core can start." >&2
    exit 127
  fi
  stash_shutdown_sentinel
  clear_shutdown_sentinel
  # errexit would exit on the failed exec before the restore below is reached;
  # drop it just around the exec and re-raise the exec's own status.
  set +e
  exec claude --name "$SESSION" ${SURFACE_ARGS[@]+"${SURFACE_ARGS[@]}"} --dangerously-skip-permissions --add-dir "$HOME" \
    ${SETTINGS_ARGS[@]+"${SETTINGS_ARGS[@]}"} ${SESSION_ARGS[@]+"${SESSION_ARGS[@]}"} \
    -- "$BOOT_PROMPT"
  _exec_rc=$?
  set -e
  restore_shutdown_sentinel
  echo "  ⚠ claude failed to exec — shutdown sentinel restored, no core is live." >&2
  exit "$_exec_rc"
fi

# Explicit -S socket path so Sutando.app (which runs under a different
# TMPDIR due to macOS sandboxing when launched via `open`) can reach the
# same tmux server as the user shell (per #PR_444 watcher-auto-restart).
#
# Sutando-friendly tmux defaults — applied to the server before the session
# attaches (see apply_claude_tmux_defaults in session-launch.sh for the full
# rationale).
#
# Tradeoff: `mouse on` intercepts native Cmd+drag text selection in the pane.
# To copy text the macOS-native way, hold Option while dragging (Terminal.app,
# iTerm2, Ghostty all honor Option-drag as a tmux-bypass). Documenting here
# so future readers don't think this is a regression.
apply_claude_tmux_defaults
#
# Branch on whether we have a TTY:
#   - TTY (user running from terminal): exec attach so the user sees the
#     Claude Code prompt and the script process IS the tmux client.
#   - No TTY (Sutando.app's Restart Core or any background invocation):
#     start detached so we don't hang, server keeps running.
#
# NOTE: the working dir (`-c` in CWD_ARGS) applies only when new-session CREATES
# the session. An existing session keeps its own start-directory, so re-anchoring
# a running core to a new working dir must go through `--restart`
# (kill-then-create), not a bare rerun.
if [ -t 1 ]; then
  ensure_core_monitor   # backgrounded child survives the exec below
  if ! launch_claude_session; then
    echo "  ⚠ $SESSION did not come up within ~5s of launch — start FAILED." >&2
    [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "FAILED: core did not come up within ~5s"
    exit 1
  fi
  clear_shutdown_sentinel
  [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "success: core live"
  ensure_task_notifier   # the supervisor needs the core session to exist first
  exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
else
  # Verify the core actually came up before reporting success. Without this a
  # failed launch (tmux server refusal, claude crash-on-start, a bad flag) still
  # exits 0 and Sutando.app reports "Core restarted" while nothing is serving —
  # the same false-success class as the --restart kill race above.
  if ! launch_claude_session; then
    echo "  ⚠ $SESSION did not come up within ~5s of launch — start FAILED." >&2
    [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "FAILED: core did not come up within ~5s"
    exit 1
  fi
  # Verified live above, so this is the first point at which clearing the
  # intentional-stop gate cannot open intake with nothing serving.
  clear_shutdown_sentinel
  [ -n "$RESTART_REQUESTED" ] && log_restart_attempt "success: core live"
  ensure_core_monitor   # canonical session now exists — start the supervisor monitor
  ensure_task_notifier
  if [ "$VISIBLE" = 1 ]; then
    open_visible_terminal
    echo "Started $SESSION detached — opened a Terminal window attached to it."
  else
    echo "Started $SESSION detached. Attach via Open Core CLI in menu bar, or:"
    echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
  fi
fi

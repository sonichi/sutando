#!/bin/bash
# Shared boot-time admission gate for both runtime launchers' task notifiers.
# Needs the same synchronous, fail-closed backfill boundary as /startup Step 1.7.

# Resolving $SUTANDO_POOL_BOOT_SWEEP is each adapter's own job, at startup --
# never here, to avoid re-globbing skills/*/manifest.json on every call.

# Both launchers forward SUTANDO_WORKSPACE_DIR/SUTANDO_TASKS_DIR into
# NOTIFIER_ENV_ARGS, so the gate must sweep the SAME tree the watcher will
# actually admit tasks from, or an override workspace's pool never gets its
# declaration checked. Delegates to workspace_dir_resolve.sh -- the single
# owner both notifier consumers also call -- rather than re-deriving it here.
_notifier_boot_gate_workspace() {
  # shellcheck source=../workspace_dir_resolve.sh
  . "$REPO/src/workspace_dir_resolve.sh" || return 1
  resolve_workspace_dir_from_tasks_dir "${SUTANDO_TASKS_DIR:-}" \
    || bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null
}

# $1: absolute path to a runnable python3 interpreter. $2 (optional): a
# workspace ALREADY resolved by the caller -- pass it so the gate sweeps the
# exact tree the caller's own identity hash and forwarded env agree on,
# instead of re-resolving independently (a second, later resolution can see
# a different answer than the first if the underlying config changes
# between calls -- one snapshot, not three). Returns 0 to proceed, non-zero
# to refuse; prints a diagnostic to stderr only on refusal.
notifier_boot_gate() {
  local py="$1" rc ws="${2:-}"
  [ -n "${SUTANDO_POOL_BOOT_SWEEP:-}" ] || return 0
  [ -n "$py" ] || return 0
  [ -n "$ws" ] || ws="$(_notifier_boot_gate_workspace)"
  "$py" "$SUTANDO_POOL_BOOT_SWEEP" --workspace "$ws" --sweep --no-persist >&2
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # A caller must not read the launcher's own exit code alone as "fully
    # up" -- the core can be fine while this run has no notifier intake path.
    echo "FATAL notifier-boot-gate: boot-time pool sweep exited $rc (not 0) -- refusing to start the task notifier's watcher, since there is no proof a live pool's routing declaration exists. The core is running WITHOUT a notifier intake path. Investigate the sweep's stderr, then re-run the launcher." >&2
    return 1
  fi
  return 0
}

# Waits up to ~0.5s for $1 to disappear (kill -0), polling every 0.05s --
# SIGKILL/SIGTERM delivery is async, so an immediate recheck can race it.
_notifier_boot_gate_await_death() {
  local pid="$1" _tries=0
  while [ "$_tries" -lt 10 ]; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.05
    _tries=$((_tries + 1))
  done
  return 1
}

# $1: workspace. $2: a runnable python3 (required). Reads $TMUX_SOCKET/
# $WATCHER_SESSION ambiently. Returns 0 once the watcher pid is confirmed dead.
# Proves identity+ownership of the sentinel pid before signaling anything.
notifier_boot_gate_force_kill_watcher() {
  local workspace="$1" py="$2" state_dir sentinel pid verdict supervisor_pid
  [ -n "$workspace" ] || return 1
  [ -n "$py" ] || return 1
  state_dir="$workspace/state"
  # shellcheck source=watcher_sentinel.sh
  . "$REPO/src/watcher_sentinel.sh" || return 1

  sentinel="$(sentinel_path_for "$state_dir")" || return 1
  [ -f "$sentinel" ] || return 1
  pid="$(cat "$sentinel" 2>/dev/null)"
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  # A pid alone cannot say WHICH process it names (reissued numbers); prove
  # BOTH watcher argv and sentinel ownership before any signal is sent.
  verdict="$("$py" -S -I "$REPO/src/watcher_identity.py" "$pid" 2>/dev/null | head -1)"
  [ "$verdict" = "watcher" ] || return 1
  sentinel_pid_wrote_file "$pid" "$sentinel" || return 1

  # Only now, with identity+ownership proven: stop the supervisor too, or
  # it respawns the watcher on the kill below within ~1s.
  if [ -n "${TMUX_SOCKET:-}" ] && [ -n "${WATCHER_SESSION:-}" ]; then
    supervisor_pid="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$WATCHER_SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
    if [ -n "$supervisor_pid" ]; then
      kill -TERM "$supervisor_pid" 2>/dev/null
      _notifier_boot_gate_await_death "$supervisor_pid" \
        || kill -KILL "$supervisor_pid" 2>/dev/null
    fi
  fi

  kill -9 "$pid" 2>/dev/null
  _notifier_boot_gate_await_death "$pid"
}

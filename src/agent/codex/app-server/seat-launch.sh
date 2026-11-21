#!/bin/bash
# Launcher half of the Codex app-server seat (SUTANDO_CODEX_APP_SERVER=1). Sourced, functions only:
# the core launcher (src/agent/codex/cli/start-cli.sh) and the pool's Codex worker launcher both
# start seat.py in the tmux session that would otherwise hold the Codex TUI.
SEAT_NAME="codex-seat"
SEAT_CLI="$REPO/src/agent/codex/app-server/$SEAT_NAME"
SEAT_PY="$REPO/src/agent/codex/app-server/seat.py"
SEAT_ARGS=()
SEAT_ENV_ARGS=()

# seat_translate_args <the TUI's codex args>...: the same launch as seat.py flags, in SEAT_ARGS.
# Every `-c key=value` is passed on to `codex app-server`; `--search` is config there, not a flag.
seat_translate_args() {
  SEAT_ARGS=()
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -c) SEAT_ARGS+=(-c "$2"); shift 2 ;;
      -m) SEAT_ARGS+=(--model "$2"); shift 2 ;;
      --sandbox) SEAT_ARGS+=(--sandbox "$2"); shift 2 ;;
      --search) SEAT_ARGS+=(-c 'web_search="live"'); shift ;;
      -C|--add-dir|--ask-for-approval) shift 2 ;;
      *) shift ;;
    esac
  done
  [ "${SUTANDO_SEAT_WATCHER:-1}" = 0 ] || SEAT_ARGS+=(--watcher)
}

# The path of a seat's host declaration: <workspace>/state/codex-seat/<tmux session>/host.json.
seat_state_dir() { printf '%s/state/codex-seat/%s' "$1" "$2"; }

# seat_core_prepare: the core's declaration, SEAT_ARGS and SEAT_ENV_ARGS. Needs the core launcher's
# CODEX_ARGS, WORKING_DIR, SESSION, TMUX_SOCKET and heartbeat_python.
seat_core_prepare() {
  local py ws tasks results state codex_bin
  py="$(heartbeat_python)"
  [ -n "$py" ] || { echo "start-cli: no runnable Python for the Codex app-server seat" >&2; return 1; }
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" && [ -n "$ws" ] \
    || { echo "start-cli: no workspace for the Codex app-server seat" >&2; return 1; }
  codex_bin="$(command -v codex)" || { echo "start-cli: codex not found for the Codex app-server seat" >&2; return 1; }
  tasks="${SUTANDO_TASKS_DIR:-$ws/tasks}"
  tasks="${tasks/#\~/$HOME}"
  results="${SUTANDO_RESULTS_DIR:-$ws/results}"
  state="$(seat_state_dir "$ws" "$SESSION")"
  "$py" "$SEAT_PY" declare --out "$state/host.json" --inbox "$tasks" --results "$results" \
    --state "$state" --cwd "$WORKING_DIR" || return 1
  seat_translate_args "${CODEX_ARGS[@]}"
  SEAT_ARGS=(--host "$state/host.json" --workspace "$ws" --seat "$SESSION" --codex "$codex_bin"
             --log "$ws/logs/codex-seat.log" "${SEAT_ARGS[@]}")
  # The seat's standby watcher reads what the notifier session's watcher reads today.
  SEAT_ENV_ARGS=(-e SUTANDO_CODEX_APP_SERVER=1 -e "SUTANDO_PY=$py" -e "PATH=$PATH"
                 -e "SUTANDO_TMUX_SOCKET=$TMUX_SOCKET" -e "SUTANDO_TMUX_SESSION=$SESSION"
                 -e SUTANDO_STANDBY_MODE=type -e "SUTANDO_STOP_INTERRUPT=${SUTANDO_STOP_INTERRUPT:-}")
  local forwarded
  for forwarded in SUTANDO_TASK_EVENT_HANDLER SUTANDO_ISOLATED_WORKING_DIR SUTANDO_CORE_MODEL \
                   SUTANDO_TASKS_DIR SUTANDO_RESULTS_DIR; do
    [ -n "${!forwarded:-}" ] && SEAT_ENV_ARGS+=(-e "$forwarded=${!forwarded}")
  done
  return 0
}

# No notifier and no observer beside a seat: a helper session left by a TUI core is ended.
seat_end_helper_sessions() {
  local helper
  for helper in "$WATCHER_SESSION" "$OBSERVER_SESSION"; do
    session_exists "$helper" && tmux -S "$TMUX_SOCKET" kill-session -t "=$helper" 2>/dev/null
  done
  return 0
}

seat_start_core_session_and_helpers() {
  seat_core_prepare || return 1
  prepare_codex_home
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" "${CORE_ENV_ARGS[@]}" "${SEAT_ENV_ARGS[@]}" \
    "$SEAT_CLI" "${SEAT_ARGS[@]}"
  wait_core_up "$SESSION" session_exists "$SESSION" || return 1
  log_session_start
  record_confirmed_core_runtime codex "$(heartbeat_python)"
  clear_shutdown_sentinel
  seat_end_helper_sessions
  ensure_core_monitor
  ensure_core_heartbeat
}

# The session outlived its seat window: recreate it as the lowest window, siblings untouched.
seat_heal_core_window() {
  local idx low others
  if others="$(sutando_unidentified_live_windows "$TMUX_SOCKET" "$SESSION" "$SEAT_NAME")" && [ -n "$others" ]; then
    echo "  ⚠ $SESSION has live windows ($(echo $others)) that are not the Codex app-server seat — not healing, a second core would double-poll. To move a running core onto the seat, run this launcher with --restart from outside the core." >&2
    return 3
  fi
  echo "  ⚠ $SESSION exists but its Codex app-server seat is gone — healing the core window (sibling windows preserved)" >&2
  seat_core_prepare || return 1
  for _win in $(sutando_core_window_ids "$TMUX_SOCKET" "$SESSION" all "$SEAT_NAME"); do
    tmux -S "$TMUX_SOCKET" kill-window -t "$_win" 2>/dev/null || true
  done
  prepare_codex_home
  if ! idx="$(sutando_new_core_window "$TMUX_SOCKET" "$SESSION" "${CORE_ENV_ARGS[@]}" "${SEAT_ENV_ARGS[@]}" -c "$REPO" "$SEAT_CLI" "${SEAT_ARGS[@]}")" \
     || [ -z "$idx" ]; then
    echo "  ⚠ could not create a seat window in $SESSION — sentinel NOT cleared, no core is serving." >&2
    return 1
  fi
  low="$(tmux -S "$TMUX_SOCKET" list-windows -t "=$SESSION" -F '#{window_index}' 2>/dev/null | sort -n | head -1)"
  if [ -n "$low" ] && [ "$low" != "$idx" ]; then
    tmux -S "$TMUX_SOCKET" swap-window -d -s "=$SESSION:$idx" -t "=$SESSION:$low" 2>/dev/null && idx="$low"
  fi
  tmux -S "$TMUX_SOCKET" select-window -t "=$SESSION:$idx" 2>/dev/null || true
  tmux -S "$TMUX_SOCKET" set-environment -t "=$SESSION" SUTANDO_CORE_RUNTIME codex 2>/dev/null || true
  wait_core_up "the healed seat window" codex_core_live || return 1
  log_session_start
  record_confirmed_core_runtime codex "$(heartbeat_python)"
  clear_shutdown_sentinel
  seat_end_helper_sessions
  ensure_core_monitor
  ensure_core_heartbeat
}

# seat_core_overrides: with the flag on, the core launcher's own steps act on the seat's window.
seat_core_overrides() {
  if ! tmux_available; then
    echo "start-cli: SUTANDO_CODEX_APP_SERVER=1 needs tmux; unset it to run the Codex TUI directly" >&2
    exit 1
  fi
  codex_core_live() {
    local ids
    ids="$(sutando_core_window_ids "$TMUX_SOCKET" "$SESSION" live "$SEAT_NAME")" && [ -n "$ids" ]
  }
  codex_core_missing() {
    local ids
    ids="$(sutando_core_window_ids "$TMUX_SOCKET" "$SESSION" live "$SEAT_NAME")" && [ -z "$ids" ]
  }
  ensure_task_notifier() { seat_end_helper_sessions; }
  ensure_codex_observer() { :; }
}

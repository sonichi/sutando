#!/bin/bash
# Launcher half of the Codex app-server seat (SUTANDO_CODEX_APP_SERVER=1). Sourced, functions only:
# the core launcher (src/agent/codex/cli/start-cli.sh) and the pool's Codex worker launcher both
# start seat.py in the tmux session that would otherwise hold the Codex TUI.
SEAT_NAME="codex-seat"
SEAT_CLI="$REPO/src/agent/codex/app-server/$SEAT_NAME"
SEAT_PY="$REPO/src/agent/codex/app-server/seat.py"
SEAT_ARGS=()
SEAT_ENV_ARGS=()
SEAT_READY=""

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

# seat_set_model <model or empty>: SEAT_ARGS with its --model replaced (or dropped when empty).
seat_set_model() {
  local out=() i=0
  while [ "$i" -lt "${#SEAT_ARGS[@]}" ]; do
    if [ "${SEAT_ARGS[$i]}" = --model ]; then i=$(( i + 2 )); continue; fi
    out+=("${SEAT_ARGS[$i]}"); i=$(( i + 1 ))
  done
  [ -n "$1" ] && out+=(--model "$1")
  SEAT_ARGS=("${out[@]}")
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
  SEAT_READY="$state/ready"
  "$py" "$SEAT_PY" declare --out "$state/host.json" --inbox "$tasks" --results "$results" \
    --state "$state" --cwd "$WORKING_DIR" || return 1
  seat_translate_args "${CODEX_ARGS[@]}"
  # A pick not yet verified is never the seat's launch model: it starts on the working one and
  # tries the pick on a turn (seat.py turn_model).
  local core_model="${SUTANDO_CORE_MODEL:-}"
  if [ -n "${CODEX_CORE_MODEL_WORKING+x}" ]; then
    core_model="$CODEX_CORE_MODEL_WORKING"
    seat_set_model "$core_model"
  fi
  SEAT_ARGS=(--host "$state/host.json" --workspace "$ws" --seat "$SESSION" --codex "$codex_bin"
             --log "$ws/logs/codex-seat.log" "${SEAT_ARGS[@]}")
  # The seat's standby watcher reads what the notifier session's watcher reads today.
  SEAT_ENV_ARGS=(-e SUTANDO_CODEX_SEAT=1 -e "SUTANDO_PY=$py" -e "PATH=$PATH"
                 -e "SUTANDO_TMUX_SOCKET=$TMUX_SOCKET" -e "SUTANDO_TMUX_SESSION=$SESSION"
                 -e SUTANDO_STANDBY_MODE=type -e "SUTANDO_STOP_INTERRUPT=${SUTANDO_STOP_INTERRUPT:-}")
  local forwarded
  for forwarded in SUTANDO_TASK_EVENT_HANDLER SUTANDO_ISOLATED_WORKING_DIR SUTANDO_CORE_MODEL_REV \
                   SUTANDO_TASKS_DIR SUTANDO_RESULTS_DIR; do
    [ -n "${!forwarded:-}" ] && SEAT_ENV_ARGS+=(-e "$forwarded=${!forwarded}")
  done
  # Only an explicit launch model is pinned in the session; one read from the record is not, so a
  # restart from inside the core reads the record again.
  [ -z "${CODEX_CORE_MODEL_WORKING+x}" ] && [ -n "$core_model" ] && SEAT_ENV_ARGS+=(-e "SUTANDO_CORE_MODEL=$core_model")
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

# seat_wait_ready: until the seat reports its app-server and thread up (seat.py writes $SEAT_READY),
# its session ends, or SUTANDO_SEAT_READY_TIMEOUT seconds (60) pass. Intake stays shut on failure.
seat_wait_ready() {
  local limit="${SUTANDO_SEAT_READY_TIMEOUT:-60}" i=0
  case "$limit" in ''|*[!0-9]*) limit=60 ;; esac
  while [ "$i" -lt $(( limit * 5 )) ]; do
    [ -f "$SEAT_READY" ] && return 0
    session_exists "$SESSION" || break
    sleep 0.2
    i=$(( i + 1 ))
  done
  [ -f "$SEAT_READY" ] && return 0
  echo "  ⚠ the Codex app-server seat did not report ready (see logs/codex-seat.log); sentinel NOT cleared. To run the Codex TUI instead: bash scripts/sutando-config.sh codex-app-server-select off (or SUTANDO_CODEX_APP_SERVER=0 for one launch)." >&2
  return 1
}

# seat_target_state <session|window> <tmux id>: alive, absent or unknown. A failed listing is unknown,
# except with no tmux server socket at all, where nothing can be alive.
seat_target_state() {
  local kind="$1" target="$2" ids
  if [ "$kind" = session ]; then
    ids="$(tmux -S "$TMUX_SOCKET" list-sessions -F '#{session_id}' 2>/dev/null)"
  else
    ids="$(tmux -S "$TMUX_SOCKET" list-windows -a -F '#{window_id}' 2>/dev/null)"
  fi || { [ -S "$TMUX_SOCKET" ] && echo unknown || echo absent; return 0; }
  printf '%s\n' "$ids" | grep -qxF -- "$target" && echo alive || echo absent
}

# seat_hold_intake: shut intake for a seat that could not be stopped, and remember which stop is ours:
# intake-held holds the token of a sentinel this launch wrote, `external` when another stop already
# held it, `unknown` when writing the stop failed (who holds intake is not known).
seat_hold_intake() {
  local py token="" rc=1
  [ -n "$SEAT_READY" ] || return 0
  py="$(heartbeat_python)"
  # Exit 0 with a token: this launch wrote the stop; 0 with none: one was already held; else failed.
  [ -n "$py" ] && { token="$("$py" "$REPO/src/shutdown.py" mark-if-absent codex-seat-not-ready 2>/dev/null)" && rc=0 || rc=$?; }
  if [ "$rc" != 0 ]; then
    token=unknown
    echo "  ✗ could not shut intake for the surviving seat (shutdown.py mark-if-absent: ${py:+exit $rc}${py:-no Python}) — task intake may still be open." >&2
  fi
  printf '%s\n' "${token:-external}" > "${SEAT_READY%/ready}/intake-held" 2>/dev/null \
    || echo "  ✗ could not write ${SEAT_READY%/ready}/intake-held; a later launch will not know this seat failed." >&2
}

# seat_stop <session|window> <tmux id>: stop exactly the seat this launch started (its immutable tmux
# id) and confirm it is gone; loud and nonzero when it is not, with intake marked held for this seat.
seat_stop() {
  local kind="$1" target="$2" state=unknown
  if [ -n "$target" ]; then
    tmux -S "$TMUX_SOCKET" "kill-$kind" -t "$target" 2>/dev/null || true
    state="$(seat_target_state "$kind" "$target")"
    [ "$state" = absent ] && return 0
  fi
  seat_hold_intake
  echo "  ✗ could not stop the unready Codex app-server seat ($kind ${target:-of unknown id}: $state) — intake stays shut; stop it by hand (tmux -S $TMUX_SOCKET kill-$kind -t ${target:-=$SESSION}) before the next launch." >&2
  return 1
}

# seat_reattach: a launch that finds the seat running accepts it as before, unless this launcher held
# intake shut for that seat (intake-held: its teardown failed). Then it must report ready first, and
# only the stop this launcher wrote is cleared (compare-and-clear on its token); a newer one stays.
seat_reattach() {
  local ws held py token rc
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || ws=""
  [ -n "$ws" ] || return 0
  SEAT_READY="$(seat_state_dir "$ws" "$SESSION")/ready"
  held="${SEAT_READY%/ready}/intake-held"
  [ -f "$held" ] || return 0
  if ! seat_wait_ready; then
    echo "  ✗ the running Codex app-server seat in $SESSION is not ready — not accepting it; intake left shut. Restart it from outside the core: bash src/agent/start-cli.sh --restart" >&2
    return 1
  fi
  py="$(heartbeat_python)"
  if [ ! -r "$held" ] || ! token="$(head -n1 -- "$held" 2>/dev/null)"; then
    echo "  ✗ could not read $held — keeping it; not accepting the seat." >&2
    return 1
  fi
  case "$token" in
    external) rm -f "$held"; return 0 ;;
    ""|unknown)
      echo "  ✗ $held does not say which stop holds intake (it could not be recorded) — keeping it; not accepting the seat. Inspect the stop in place first (cat \"\$(python3 src/shutdown.py path)\" and python3 src/shutdown.py token); only if it is this seat's codex-seat-not-ready stop and intake should reopen, run python3 src/shutdown.py clear-if <that token>. Then remove the marker: rm '$held'." >&2
      return 1 ;;
  esac
  rc=1
  [ -n "$py" ] && { "$py" "$REPO/src/shutdown.py" clear-if "$token" >/dev/null 2>&1 && rc=0 || rc=$?; }
  case "$rc" in
    0) ;;
    3) echo "  · the Codex app-server seat is ready; a newer stop holds intake, so it stays shut." >&2 ;;
    *) echo "  ✗ the Codex app-server seat is ready, but the intake held for it could not be reopened (shutdown.py clear-if: ${py:+exit }${py:+$rc}${py:-no Python}) — keeping $held; not accepting the seat." >&2
       return 1 ;;
  esac
  rm -f "$held"
}

# seat_note_stop: the token of the stop in place as this launch begins (SEAT_STOP_AT_START: empty for
# none, `unknown` when unreadable). Only that stop is lifted once the seat is ready; a later one stays.
seat_note_stop() {
  local py
  SEAT_STOP_AT_START=unknown
  py="$(heartbeat_python)"
  [ -n "$py" ] && SEAT_STOP_AT_START="$("$py" "$REPO/src/shutdown.py" token 2>/dev/null)" || SEAT_STOP_AT_START=unknown
}

# seat_lift_stop: compare-and-clear of the stop seat_note_stop saw; a drain written since is kept.
seat_lift_stop() {
  local py rc=1
  case "${SEAT_STOP_AT_START-unknown}" in
    "") return 0 ;;
    unknown)
      echo "  ⚠ could not read the stop in place when this launch began — sentinel NOT cleared; inspect it (python3 src/shutdown.py path) before reopening intake." >&2
      return 0 ;;
  esac
  py="$(heartbeat_python)"
  [ -n "$py" ] && { "$py" "$REPO/src/shutdown.py" clear-if "$SEAT_STOP_AT_START" >/dev/null 2>&1 && rc=0 || rc=$?; }
  case "$rc" in
    0) ;;
    3) echo "  · a stop written while the seat started holds intake, so it stays shut." >&2 ;;
    *) echo "  ⚠ shutdown.py clear-if failed (${py:+exit $rc}${py:-no Python}) — the intake gate may hold tasks." >&2 ;;
  esac
  return 0
}

seat_start_core_session_and_helpers() {
  local seat_id
  seat_note_stop
  seat_core_prepare || return 1
  prepare_codex_home
  rm -f "$SEAT_READY" "${SEAT_READY%/ready}/intake-held"
  seat_id="$(tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" "${CORE_ENV_ARGS[@]}" "${SEAT_ENV_ARGS[@]}" \
    -P -F '#{session_id}' "$SEAT_CLI" "${SEAT_ARGS[@]}")" || seat_id=""
  wait_core_up "$SESSION" session_exists "$SESSION" || return 1
  if ! seat_wait_ready; then
    # Torn down, not left starting: a later launch would find it live and skip this readiness check.
    seat_stop session "$seat_id"
    return 1
  fi
  log_session_start
  record_confirmed_core_runtime codex "$(heartbeat_python)"
  seat_lift_stop
  seat_end_helper_sessions
  ensure_core_monitor
  ensure_core_heartbeat
}

# The session outlived its seat window: recreate it as the lowest window, siblings untouched.
seat_heal_core_window() {
  local idx low others
  seat_note_stop
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
  rm -f "$SEAT_READY" "${SEAT_READY%/ready}/intake-held"
  local created win_id
  # The window's immutable id comes back from the call that creates it, with its index.
  created="$(tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_id} #{window_index}' -t "=$SESSION:0" \
      "${CORE_ENV_ARGS[@]}" "${SEAT_ENV_ARGS[@]}" -c "$REPO" "$SEAT_CLI" "${SEAT_ARGS[@]}" 2>/dev/null \
    || tmux -S "$TMUX_SOCKET" new-window -dP -F '#{window_id} #{window_index}' -t "=$SESSION:" \
      "${CORE_ENV_ARGS[@]}" "${SEAT_ENV_ARGS[@]}" -c "$REPO" "$SEAT_CLI" "${SEAT_ARGS[@]}")" || created=""
  win_id="${created%% *}"
  idx="${created#* }"
  if [ -z "$created" ] || [ -z "$win_id" ] || [ "$idx" = "$created" ]; then
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
  if ! seat_wait_ready; then
    seat_stop window "$win_id"
    return 1
  fi
  log_session_start
  record_confirmed_core_runtime codex "$(heartbeat_python)"
  seat_lift_stop
  seat_end_helper_sessions
  ensure_core_monitor
  ensure_core_heartbeat
}

# seat_core_overrides: with the flag on, the core launcher's own steps act on the seat's window.
seat_core_overrides() {
  if ! tmux_available; then
    echo "start-cli: the Codex app-server seat needs tmux; to run the Codex TUI instead: bash scripts/sutando-config.sh codex-app-server-select off (or SUTANDO_CODEX_APP_SERVER=0 for one launch)" >&2
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
  # Called only on the launcher's already-running path while a seat is the core.
  ensure_task_notifier() { seat_end_helper_sessions; seat_reattach || exit 1; }
  ensure_codex_observer() { :; }
  # The supervisor's block hooks read a TUI pane, and the seat draws none.
  add_codex_block_hooks() { :; }
}

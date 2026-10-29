#!/bin/bash
# Core-window policy shared by the runtime launchers: a restart or heal replaces only the
# core's own window, so sibling windows in the session (gateway lanes, monitor) keep running.

# sutando_core_window_ids <socket> <session> <live|all> <cli>...: ids of windows whose pane
# was started as one of the named CLIs (`live`: still running). Fails when panes are unreadable.
sutando_core_window_ids() {
  local socket="$1" session="$2" mode="$3" rows wid dead cmd rt
  shift 3
  rows="$(tmux -S "$socket" list-panes -s -t "=$session" -F '#{window_id} #{pane_dead} #{pane_start_command}' 2>/dev/null)" \
    || return 1
  printf '%s\n' "$rows" \
    | while read -r wid dead cmd _; do
        [ "$mode" = all ] || [ "$dead" = 0 ] || continue
        cmd="${cmd#\"}"; cmd="${cmd%\"}"
        for rt in "$@"; do
          if [ "${cmd##*/}" = "$rt" ]; then echo "$wid"; break; fi
        done
      done | sort -u
}

# sutando_new_core_window <socket> <session> <new-window args>...: create the core window in
# the background at index 0, its conventional home, else any free index; prints the index.
sutando_new_core_window() {
  local socket="$1" session="$2"
  shift 2
  tmux -S "$socket" new-window -dP -F '#{window_index}' -t "$session:0" "$@" 2>/dev/null \
    || tmux -S "$socket" new-window -dP -F '#{window_index}' -t "$session" "$@"
}

# sutando_unidentified_live_windows <socket> <session> <cli>...: ids of live windows that are
# neither a named CLI nor a sibling (`gateway-*`, `monitor`) running a non-core command: a core
# the command match cannot see. Fails when panes are unreadable.
sutando_unidentified_live_windows() {
  local socket="$1" session="$2" rows wid dead name cmd rt known
  shift 2
  rows="$(tmux -S "$socket" list-panes -s -t "=$session" -F '#{window_id}|#{pane_dead}|#{window_name}|#{pane_start_command}' 2>/dev/null)" \
    || return 1
  printf '%s\n' "$rows" \
    | while IFS='|' read -r wid dead name cmd; do
        [ "$dead" = 0 ] || continue
        cmd="${cmd#\"}"; cmd="${cmd%%\"*}"; cmd="${cmd%% *}"
        # A sibling's name is creator-assigned, so a core CLI under that name still counts.
        case "$name:${cmd##*/}" in gateway-*:claude|gateway-*:codex|monitor:claude|monitor:codex) ;; gateway-*:*|monitor:*) continue ;; esac
        known=
        for rt in "$@"; do [ "${cmd##*/}" = "$rt" ] && known=1; done
        [ -n "$known" ] || echo "$wid"
      done | sort -u
}

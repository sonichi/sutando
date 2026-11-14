#!/bin/bash
# Called only after the adapter confirms its named core is present.
record_confirmed_core_runtime() {
  local runtime="$1" py="$2" ws started
  persist_confirmed_desktop_selection "$runtime"
  [ -z "${SUTANDO_INSTANCE_ID:-}" ] || return 0
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  if [ -z "$py" ]; then
    echo "start-cli: no Python available to record the confirmed core runtime" >&2
    return 0
  fi
  started="$(tmux -S "$TMUX_SOCKET" display-message -p -t "=$SESSION:" '#{session_created}' 2>/dev/null || true)"
  case "$started" in
    ''|*[!0-9]*)
      echo "start-cli: session creation time unavailable; runtime record unchanged" >&2
      return 0
      ;;
  esac
  # The writer itself confirms the panes run this runtime's CLI and says why when not.
  "$py" "$REPO/src/core_runtime_record.py" "$ws" "$runtime" "$SESSION" "$started" \
    --socket "$TMUX_SOCKET" || true
}

# The dispatcher sets SUTANDO_DESKTOP_RUNTIME_SELECT for an explicit switch on the desktop's
# socket, after checking the selection is writable; it is written only for a confirmed core.
persist_confirmed_desktop_selection() {
  local runtime="$1" selected
  [ "${SUTANDO_DESKTOP_RUNTIME_SELECT:-}" = "$runtime" ] || return 0
  unset SUTANDO_DESKTOP_RUNTIME_SELECT
  if selected="$(bash "$REPO/scripts/sutando-config.sh" desktop-runtime-select "$runtime")"; then
    [ "$selected" = updated ] && echo "start-cli: desktop runtime selection is now $runtime." >&2
  else
    echo "start-cli: $runtime core is live, but the desktop's runtime selection could not be written." >&2
  fi
  return 0
}

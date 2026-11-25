#!/bin/bash
# Called only after the adapter confirms its named core is present.
record_confirmed_core_runtime() {
  # The lock goes last, on every path: whoever was waiting for it then reads what was published here,
  # the selection and the confirmed core, and never the state from before this launch.
  CORE_RUNTIME_PUBLISHED=0
  publish_confirmed_core_runtime "$@"
  release_restart_lock_of_this_launch "$1"
}

publish_confirmed_core_runtime() {
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
  if "$py" "$REPO/src/core_runtime_record.py" "$ws" "$runtime" "$SESSION" "$started" \
    --socket "$TMUX_SOCKET"; then
    CORE_RUNTIME_PUBLISHED=1
  fi
  return 0
}

# graceful-restart.sh exec's the launcher still holding its lock (GR_RID names it). A confirmed core is
# the end of that restart: the lock goes now, not when it ages out, or the next restart is put off.
# What this restart came to is left first, under its own id: {rid, runtime, published, at}, where
# `published` says whether the confirmed-core record was written. A reader that waited on the lock
# ties what it reads to this restart by that id, and reads a failed publication as one.
release_restart_lock_of_this_launch() {
  [ -n "${GR_RID:-}" ] || return 0
  local runtime="${1:-}" ws lock result rid="$GR_RID" published=false
  unset GR_RID
  ws="$(bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 0
  [ -n "$ws" ] || return 0
  lock="$ws/state/locks/graceful-restart.lock"
  # Only this run's own lock: one a later restart took (after reaping ours) is left alone.
  [ "$(cat "$lock/rid" 2>/dev/null)" = "$rid" ] || return 0
  [ "${CORE_RUNTIME_PUBLISHED:-0}" = 1 ] && published=true
  result="$ws/state/graceful-restart-result.json"
  case "$rid$runtime" in
    *[!A-Za-z0-9._-]*) ;;   # nothing that is not plain is written into the JSON
    *) printf '{"rid": "%s", "runtime": "%s", "published": %s, "at": %s}\n' \
         "$rid" "$runtime" "$published" "$(date +%s)" > "$result.tmp" 2>/dev/null \
         && mv -f "$result.tmp" "$result" 2>/dev/null ;;
  esac
  rm -rf "$lock"
  return 0
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

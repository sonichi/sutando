#!/bin/bash
# Optional capability lookup for the watcher's task-event handler. A skill that
# provides one publishes an executable at skills/<skill>/task-event-handler
# (a symlink to its script is fine). This helper names no skill: exactly one
# publisher wins; none sets nothing; several is ambiguous and sets nothing,
# loudly, so the operator pins SUTANDO_TASK_EVENT_HANDLER explicitly.
#
# resolve_task_event_handler <repo>  -> prints the path (rc 0) | rc 1 none | rc 2 ambiguous

resolve_task_event_handler() {
  local repo="$1" h
  set --
  for h in "$repo"/skills/*/task-event-handler; do
    [ -x "$h" ] && set -- "$@" "$h"
  done
  case $# in
    0) return 1 ;;
    1) printf '%s\n' "$1"; return 0 ;;
    *) printf 'task-event-handler: %s skills publish one (%s); set SUTANDO_TASK_EVENT_HANDLER explicitly\n' "$#" "$*" >&2; return 2 ;;
  esac
}

# A publisher created only at worker-registration time never re-runs for a pool
# that already existed before this repo stopped shipping the file, so each
# skill gets one chance, here, to republish before every resolution. Names no
# skill: `skills/*/task-event-handler-ensure`, same neutral glob as above.
#
# Returns nonzero if ANY hook that ran failed: a hook that could not confirm
# "no pool" and could not repair one either leaves the caller unable to tell
# a fixed pool from a broken one, so it must refuse rather than resolve.
ensure_task_event_handlers_published() {
  local repo="$1" ensure rc=0
  for ensure in "$repo"/skills/*/task-event-handler-ensure; do
    [ -x "$ensure" ] || continue
    "$ensure" "$repo" || rc=1
  done
  return "$rc"
}

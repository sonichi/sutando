#!/bin/bash
# Optional capability lookup for the watcher's task-event handler. A skill that
# provides one publishes an executable at skills/<skill>/task-event-handler
# (a symlink to its script is fine). This helper names no skill: exactly one
# publisher wins; none sets nothing; several is ambiguous and sets nothing,
# loudly, so the operator pins SUTANDO_TASK_EVENT_HANDLER explicitly.
#
# resolve_task_event_handler <repo>  -> prints the path (rc 0) | rc 1 none | rc 2 ambiguous

TASK_EVENT_HANDLER_CAPABILITY="SUTANDO_TASK_EVENT_HANDLER_SCRIPT"

# Kept as a -c program, not a heredoc inside a command substitution: the nested
# form works but re-nests wrongly under a small edit, and does so silently.
read -r -d '' __TASK_EVENT_HANDLER_PROG <<'PYPROG' || true
import json, os, sys
repo, key = sys.argv[1], sys.argv[2]
skills = os.path.join(repo, "skills")
for name in sorted(os.listdir(skills) if os.path.isdir(skills) else []):
    manifest = os.path.join(skills, name, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh:
            config = json.load(fh).get("config") or {}
    except (OSError, ValueError):
        continue
    declared = config.get(key) if isinstance(config, dict) else None
    if not isinstance(declared, str) or not declared:
        continue
    path = declared if os.path.isabs(declared) else os.path.join(skills, name, declared)
    # realpath, not normpath: normpath folds `..` but follows no symlink, and the
    # executable test does, so a link out of the skill would pass a string check.
    path = os.path.realpath(path)
    own = os.path.realpath(os.path.join(skills, name))
    if not path.startswith(own + os.sep):
        print(f"task-event-handler: {manifest}: {key} escapes its skill", file=sys.stderr)
        continue
    if os.access(path, os.X_OK):
        print(path)
PYPROG

# resolve_task_event_handler <repo> -> path (rc 0) | rc 1 none | rc 2 cannot tell
#
# An interpreter that cannot run takes rc 2, the fail-closed code: a reader that
# cannot read is not evidence that nobody declared one.
resolve_task_event_handler() {
  local repo="$1" py="${SUTANDO_PY_BIN:-python3}" out prc h
  out="$("$py" -c "$__TASK_EVENT_HANDLER_PROG" "$repo" "$TASK_EVENT_HANDLER_CAPABILITY")"
  prc=$?
  if [ "$prc" -ne 0 ]; then
    printf 'task-event-handler: cannot read skill manifests (%s exited %s); refusing to report "none"\n' "$py" "$prc" >&2
    return 2
  fi
  set --
  while IFS= read -r h; do
    [ -n "$h" ] && set -- "$@" "$h"
  done <<EOF
$out
EOF
  case $# in
    0) return 1 ;;
    1) printf '%s\n' "$1"; return 0 ;;
    *) printf 'task-event-handler: %s skills declare %s (%s); set SUTANDO_TASK_EVENT_HANDLER explicitly\n' "$#" "$TASK_EVENT_HANDLER_CAPABILITY" "$*" >&2; return 2 ;;
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

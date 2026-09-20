#!/bin/bash
# Optional capability lookup for the watcher's task-event handler. A skill that
# provides one DECLARES it in its own manifest.json:
#
#     {"config": {"SUTANDO_TASK_EVENT_HANDLER_SCRIPT": "scripts/route_handler.py"}}
#
# skill-relative, and it must stay inside the declaring skill. This helper names
# no skill: exactly one declarer wins; none sets nothing; several is ambiguous
# and sets nothing, loudly, so the operator pins SUTANDO_TASK_EVENT_HANDLER.
#
# MIGRATION: a published executable at skills/<skill>/task-event-handler was the
# previous contract and is NO LONGER RESOLVED. It could not be shipped -- it is
# gitignored and had to be created at runtime -- so a correct checkout resolved
# nothing until something published it. A skill that still publishes one and
# declares no config key is named on stderr rather than silently ignored.
#
# resolve_task_event_handler <repo>  -> prints the path (rc 0) | rc 1 none | rc 2 cannot tell

TASK_EVENT_HANDLER_CAPABILITY="SUTANDO_TASK_EVENT_HANDLER_SCRIPT"

# Kept as a -c program, not a heredoc inside a command substitution: the nested
# form works but re-nests wrongly under a small edit, and does so silently.
read -r -d '' __TASK_EVENT_HANDLER_PROG <<'PYPROG' || true
import json, os, sys
repo, key = sys.argv[1], sys.argv[2]
skills = os.path.join(repo, "skills")
declared_by = set()
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
        declared_by.add(name)
for name in sorted(os.listdir(skills) if os.path.isdir(skills) else []):
    legacy = os.path.join(skills, name, "task-event-handler")
    if name not in declared_by and os.path.exists(legacy):
        print(f"task-event-handler: {legacy}: the published-file contract is no longer "
              f"resolved; declare {key} in {name}/manifest.json", file=sys.stderr)
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

# task_event_handler <repo> -> path (rc 0) | rc 1 none/broken pin | rc 2 cannot tell
#
# Call fresh for every task, never once at process start or cached in a var
# that outlives the call: an explicit pin always wins over resolution.
task_event_handler() {
  local repo="$1"
  if [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ]; then
    [ -x "$SUTANDO_TASK_EVENT_HANDLER" ] || return 1
    printf '%s\n' "$SUTANDO_TASK_EVENT_HANDLER"
    return 0
  fi
  resolve_task_event_handler "$repo"
}

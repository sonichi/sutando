#!/bin/bash
# The task-event handler is declared by writing a small JSON config file:
#
#     <workspace>/state/task-event-handler.json   {"handler": "<abs path>"}
#
# A skill's own "in use" touchpoint is the one production writer (e.g.
# worker-pool's register_worker() calls publish_task_event_handler() on every
# worker registration) -- see src/util_paths.py's task_event_handler_config_path.
# No manifest scan, no symlink: the watcher reads this one file, and a caller
# that also watches it (fswatch) can reload the moment it changes, in-process.
#
# read_task_event_handler_config <path> -> prints the handler path (rc 0) | rc 1 none/unusable

read_task_event_handler_config() {
  local cfg="$1" py="${SUTANDO_PY_BIN:-python3}"
  [ -f "$cfg" ] || return 1
  "$py" -c '
import json, os, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        handler = (json.load(fh) or {}).get("handler")
except (OSError, ValueError):
    sys.exit(1)
if not isinstance(handler, str) or not handler or not os.access(handler, os.X_OK):
    sys.exit(1)
print(handler)
' "$cfg"
}

# task_event_handler <config-path> -> path (rc 0) | rc 1 none
#
# An explicit SUTANDO_TASK_EVENT_HANDLER pin always wins over the declared
# config, checked live every call: an operator pin can be set or cleared at
# any time and must never be shadowed by a cached config-file read.
task_event_handler() {
  local cfg="$1"
  if [ -n "${SUTANDO_TASK_EVENT_HANDLER:-}" ]; then
    [ -x "$SUTANDO_TASK_EVENT_HANDLER" ] || return 1
    printf '%s\n' "$SUTANDO_TASK_EVENT_HANDLER"
    return 0
  fi
  read_task_event_handler_config "$cfg"
}

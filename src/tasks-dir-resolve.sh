#!/bin/bash
# Shared TASKS_DIR resolution — sourceable so watch-tasks-stream.sh and
# task-notifier-supervisor.sh can never resolve a different inbox for the
# same instance. Priority: explicit arg -> SUTANDO_TASKS_DIR -> canonical M0
# workspace loader. Prints the resolved path on success; prints nothing and
# returns 1 when the checkout itself can't be resolved.
resolve_tasks_dir() {
  local explicit="${1:-}" repo_root="${2:?resolve_tasks_dir: repo_root required}" ws
  if [ -n "$explicit" ]; then
    printf '%s\n' "$explicit"
    return 0
  fi
  if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
    printf '%s\n' "$SUTANDO_TASKS_DIR"
    return 0
  fi
  if [ -f "$repo_root/scripts/sutando-config.sh" ]; then
    ws="$(bash "$repo_root/scripts/sutando-config.sh" workspace)" || return 1
    printf '%s/tasks\n' "$ws"
    return 0
  fi
  return 1
}

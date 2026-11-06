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

# The physical inbox path: fswatch emits physical paths, and one directory must
# have one spelling wherever it is named.
canonical_tasks_dir() {
  (cd "$1" 2>/dev/null && pwd -P)
}
# The workspace an inbox belongs to: the one the launcher names, else the
# physical inbox's parent. The watcher stamps under it; the supervisor reads there.
workspace_dir_for_inbox() {
  local abs
  abs="$(canonical_tasks_dir "$1")" || abs="$1"
  printf '%s\n' "${SUTANDO_WORKSPACE_DIR:-$(dirname "$abs")}"
}

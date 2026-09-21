#!/bin/bash
# Single owner for "given a (possibly tilde-prefixed) tasks dir, which
# workspace does this mean" -- notifier-boot-gate.sh, task-notifier.sh
# (Codex) and watch-tasks-stream.sh (Claude) must never re-derive this
# independently; a hand-copied formula is exactly what let them disagree.

# $1: a tasks-dir value (may be empty/unset). Reads SUTANDO_WORKSPACE_DIR
# ambiently (it always wins, matching every caller's existing precedence).
# Prints nothing and returns 1 if neither input is available.
resolve_workspace_dir_from_tasks_dir() {
  local tasks_dir="$1"
  if [ -n "${SUTANDO_WORKSPACE_DIR:-}" ]; then
    printf '%s' "${SUTANDO_WORKSPACE_DIR/#\~/$HOME}"
    return 0
  fi
  [ -n "$tasks_dir" ] || return 1
  dirname "${tasks_dir/#\~/$HOME}"
}

# $1: repo root (for the config-default fallback). Prints
# "WORKSPACE_DIR|TASKS_DIR|RESULTS_DIR", matching exactly what a notifier
# consumer resolves -- including the unset-override case, where the consumer
# still lands on a real, config-derived workspace, never a blank value. A
# caller hashing only the raw SUTANDO_* strings sees a constant when they're
# unset, even though the CONFIGURED default workspace can itself change.
resolve_effective_workspace_triple() {
  local repo="$1" tasks_dir workspace_dir results_dir
  if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
    tasks_dir="${SUTANDO_TASKS_DIR/#\~/$HOME}"
  else
    tasks_dir="$(bash "$repo/scripts/sutando-config.sh" workspace 2>/dev/null)/tasks"
  fi
  workspace_dir="$(resolve_workspace_dir_from_tasks_dir "$tasks_dir")" || return 1
  results_dir="${SUTANDO_RESULTS_DIR:-$workspace_dir/results}"
  printf '%s|%s|%s' "$workspace_dir" "$tasks_dir" "$results_dir"
}

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

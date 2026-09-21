#!/bin/bash
# Single owner for "given a (possibly tilde-prefixed) tasks dir, which
# workspace does this mean" -- notifier-boot-gate.sh, task-notifier.sh
# (Codex) and watch-tasks-stream.sh (Claude) must never re-derive this
# independently; a hand-copied formula is exactly what let them disagree.

# $1: a path, possibly containing a symlinked component (e.g. macOS's
# /tmp -> /private/tmp). Prints the PHYSICAL form -- fswatch always emits
# physical paths in its event stream, so every consumer that later compares
# a resolved path against an fswatch event must agree on this representation,
# or a symlinked path silently never matches. A gate/hash/env computation is
# a READ path, not a write: this never creates anything (a mistyped override
# must still show up as a missing directory, not get silently materialized).
# Instead it canonicalizes the longest EXISTING prefix and re-appends the
# rest literally -- stable and deterministic whether or not the leaf exists
# yet, unlike "canonicalize only if it happens to exist right now" (that
# version made a same-config relaunch's restart-identity hash depend on
# whether something ELSE had created the leaf dir between the two calls).
_canonicalize_or_keep() {
  local path="$1" prefix="$1" suffix=""
  while [ -n "$prefix" ] && [ "$prefix" != "/" ] && ! [ -d "$prefix" ]; do
    suffix="/$(basename "$prefix")$suffix"
    prefix="$(dirname "$prefix")"
  done
  if [ -d "$prefix" ]; then
    printf '%s%s' "$(cd "$prefix" && pwd -P)" "$suffix"
  else
    printf '%s' "$path"
  fi
}

# $1: a tasks-dir value (may be empty/unset). Reads SUTANDO_WORKSPACE_DIR
# ambiently (it always wins, matching every caller's existing precedence).
# Always returns the PHYSICAL form (see _canonicalize_or_keep) -- an earlier
# version returned SUTANDO_WORKSPACE_DIR verbatim, which watch-tasks-stream.sh
# then compared against fswatch's physical-path events and never matched
# when the override pointed through a symlink (keweichen, round 19+).
# Prints nothing and returns 1 if neither input is available.
resolve_workspace_dir_from_tasks_dir() {
  local tasks_dir="$1" resolved
  if [ -n "${SUTANDO_WORKSPACE_DIR:-}" ]; then
    resolved="${SUTANDO_WORKSPACE_DIR/#\~/$HOME}"
  else
    [ -n "$tasks_dir" ] || return 1
    resolved="$(dirname "${tasks_dir/#\~/$HOME}")"
  fi
  _canonicalize_or_keep "$resolved"
}

# $1: repo root (for the config-default fallback). Prints WORKSPACE_DIR,
# TASKS_DIR, RESULTS_DIR as three NUL-terminated fields (never a printable
# delimiter -- a `|` is a legal path character, and keweichen's control showed
# a path containing one gets silently misparsed into the wrong tasks/results
# dir). All three are PHYSICAL (see _canonicalize_or_keep), matching exactly
# what a real consumer's own canonicalization (e.g. watch-tasks-stream.sh's
# `TASKS_DIR_ABS="$(cd "$TASKS_DIR" && pwd -P)"`) independently computes.
# Read with `mapfile -d '' -t fields < <(resolve_effective_workspace_triple ...)`,
# never `x=$(...)` -- command substitution truncates at the first NUL.
resolve_effective_workspace_triple() {
  local repo="$1" tasks_dir workspace_dir results_dir
  if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
    tasks_dir="${SUTANDO_TASKS_DIR/#\~/$HOME}"
  else
    tasks_dir="$(bash "$repo/scripts/sutando-config.sh" workspace 2>/dev/null)/tasks"
  fi
  workspace_dir="$(resolve_workspace_dir_from_tasks_dir "$tasks_dir")" || return 1
  tasks_dir="$(_canonicalize_or_keep "$tasks_dir")"
  results_dir="$(_canonicalize_or_keep "${SUTANDO_RESULTS_DIR:-$workspace_dir/results}")"
  printf '%s\0%s\0%s\0' "$workspace_dir" "$tasks_dir" "$results_dir"
}

#!/bin/bash
# Single owner for "given a (possibly tilde-prefixed) tasks dir, which
# workspace does this mean" -- notifier-boot-gate.sh, task-notifier.sh
# (Codex) and watch-tasks-stream.sh (Claude) must never re-derive this
# independently; a hand-copied formula is exactly what let them disagree.

# Pure-bash basename/dirname -- no subprocess. Under a thin/missing PATH
# (e.g. a minimal boot environment) the external basename/dirname commands
# are unresolvable; the previous version treated that failure as "no
# existing ancestor found" and fell back to the UNRESOLVED logical path,
# reintroducing the exact physical/logical mismatch this file exists to
# prevent (qingyun-wu, #4503 review, reproduced with PATH= set).
_wdr_basename() { printf '%s' "${1##*/}"; }
_wdr_dirname() {
  case "$1" in
    */*) local d="${1%/*}"; printf '%s' "${d:-/}" ;;
    *) printf '%s' "." ;;
  esac
}

# $1: a path, possibly containing a symlinked component (e.g. macOS's
# /tmp -> /private/tmp). Prints the PHYSICAL form on rc 0 -- fswatch always
# emits physical paths in its event stream, so every consumer that later
# compares a resolved path against an fswatch event must agree on this
# representation, or a symlinked path silently never matches. A gate/hash/env
# computation is a READ path, not a write: this never creates anything (a
# mistyped override must still show up as a missing directory, not get
# silently materialized). It canonicalizes the longest EXISTING prefix and
# re-appends the rest literally -- stable and deterministic whether or not
# the leaf exists yet, unlike "canonicalize only if it happens to exist
# right now" (that version made a same-config relaunch's restart-identity
# hash depend on whether something ELSE had created the leaf dir between
# the two calls).
#
# rc 1, prints NOTHING, if an existing prefix cannot actually be entered
# (e.g. permission denied). `cd` failing inside a bare command substitution
# used to be swallowed -- `printf` ran regardless, on an empty captured
# string -- so a chmod-000 ancestor produced a SUCCESSFUL, EMPTY resolution
# that every downstream caller treated as valid (keweichen, #4503 review,
# P1: an empty workspace field silently reached notifier_boot_gate.sh,
# which reads an empty string as "sweep the caller's cwd", reports success
# finding nothing there, and the notifier then admits worker-owned tasks
# into the unrestricted core). Every caller MUST check this function's
# return code -- a path that cannot be validated must never be treated as
# resolved.
_canonicalize_or_keep() {
  local path="$1" prefix="$1" suffix="" physical
  case "$prefix" in
    /*) ;;
    *) printf '%s' "$path"; return 0 ;;
  esac
  while [ -n "$prefix" ] && [ "$prefix" != "/" ] && ! [ -d "$prefix" ]; do
    suffix="/$(_wdr_basename "$prefix")$suffix"
    prefix="$(_wdr_dirname "$prefix")"
  done
  if [ -d "$prefix" ]; then
    physical="$(cd "$prefix" 2>/dev/null && pwd -P)" || return 1
    [ -n "$physical" ] || return 1
    printf '%s%s' "$physical" "$suffix"
    return 0
  fi
  printf '%s' "$path"
}

# $1: a tasks-dir value (may be empty/unset). Reads SUTANDO_WORKSPACE_DIR
# ambiently (it always wins, matching every caller's existing precedence).
# Prints the PHYSICAL form (see _canonicalize_or_keep) on rc 0 -- an earlier
# version returned SUTANDO_WORKSPACE_DIR verbatim, which watch-tasks-stream.sh
# then compared against fswatch's physical-path events and never matched
# when the override pointed through a symlink (keweichen, round 19+).
# Prints nothing and returns 1 if neither input is available, OR if
# canonicalization could not validate the resolved path (see
# _canonicalize_or_keep) -- a caller must never substitute empty output
# for a real resolution.
resolve_workspace_dir_from_tasks_dir() {
  local tasks_dir="$1" resolved
  if [ -n "${SUTANDO_WORKSPACE_DIR:-}" ]; then
    resolved="${SUTANDO_WORKSPACE_DIR/#\~/$HOME}"
  else
    [ -n "$tasks_dir" ] || return 1
    resolved="$(_wdr_dirname "${tasks_dir/#\~/$HOME}")"
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
#
# rc 1 and prints NOTHING if any of the three cannot be resolved -- a
# partial or empty-fielded triple must never reach a caller as if it were
# valid, so every field is gated with `|| return 1`, not just the first
# (keweichen, #4503 review, P1 -- see _canonicalize_or_keep).
#
# The config-default fallback (no SUTANDO_TASKS_DIR) is its own gate: a
# failing or silent `sutando-config.sh workspace` used to be concatenated
# straight into "$(...)/tasks" unchecked -- a failure or empty stdout left
# tasks_dir as literally "/tasks", which _canonicalize_or_keep then resolves
# to a non-empty "//tasks" (walking up to "/", the one ancestor that always
# exists). That is NON-empty, so it passed every empty-field guard, and a
# watcher could start rooted at the filesystem root instead of refusing
# (qingyun-wu, #4503 review, withdrew approval at 363bb0d15 over exactly
# this). Fixed by capturing the config command's own output and checking
# both its exit status and that it printed something, before ever building
# a path from it.
resolve_effective_workspace_triple() {
  local repo="$1" tasks_dir workspace_dir results_dir config_ws
  if [ -n "${SUTANDO_TASKS_DIR:-}" ]; then
    tasks_dir="${SUTANDO_TASKS_DIR/#\~/$HOME}"
  else
    config_ws="$(bash "$repo/scripts/sutando-config.sh" workspace 2>/dev/null)" || return 1
    [ -n "$config_ws" ] || return 1
    tasks_dir="$config_ws/tasks"
  fi
  workspace_dir="$(resolve_workspace_dir_from_tasks_dir "$tasks_dir")" || return 1
  tasks_dir="$(_canonicalize_or_keep "$tasks_dir")" || return 1
  results_dir="$(_canonicalize_or_keep "${SUTANDO_RESULTS_DIR:-$workspace_dir/results}")" || return 1
  printf '%s\0%s\0%s\0' "$workspace_dir" "$tasks_dir" "$results_dir"
}

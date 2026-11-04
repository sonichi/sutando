#!/bin/bash
# Launch-PATH policy shared by the runtime launchers and health-check's restart env:
# the engine-owned directories a core launch needs that a launchd job's PATH lacks.

# Sourced, defines the functions below; executed with <repo>, prints the directories.

# sutando_launch_path_dirs <repo>: candidate dirs, one per line, existing or not.
# The desktop app ships node at <engine>/runtime/node/bin; runtime/bin is the older layout.
sutando_launch_path_dirs() {
  local engine="$1/.."
  printf '%s\n' "$engine/runtime/node/bin" "$engine/runtime/bin" "$HOME/.local/bin"
}

# sutando_append_launch_path <repo> [extra-dir...]: append each existing dir not already
# on PATH, standard dirs first. Appends, so a node or CLI the caller put on PATH keeps winning.
sutando_append_launch_path() {
  local repo="$1" d
  shift
  while IFS= read -r d; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    d="$(cd "$d" && pwd)"
    case ":$PATH:" in
      *":$d:"*) ;;
      *) PATH="${PATH:+$PATH:}$d" ;;
    esac
  done < <(sutando_launch_path_dirs "$repo"; [ "$#" -gt 0 ] && printf '%s\n' "$@")
  export PATH
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ -n "${1:-}" ] || { echo "usage: launch-path.sh <repo>" >&2; exit 2; }
  sutando_launch_path_dirs "$1"
fi

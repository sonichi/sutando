#!/bin/bash
# Launch-PATH policy shared by the runtime launchers and health-check's restart env:
# the engine-owned directories a core launch needs that a launchd job's PATH lacks.

# Sourced, defines the functions below; executed with <repo>, prints the directories.

# sutando_bundled_node_dir <repo>: where the desktop app ships node.
sutando_bundled_node_dir() {
  printf '%s\n' "$1/../runtime/node/bin"
}

# sutando_launch_path_dirs <repo>: default dirs, one per line, existing or not.
# runtime/bin is the older bundle layout; ~/.local/bin is where `claude` installs.
sutando_launch_path_dirs() {
  sutando_bundled_node_dir "$1"
  printf '%s\n' "$1/../runtime/bin" "$HOME/.local/bin"
}

# sutando_append_launch_path <repo> [override-dir...]: append each existing dir not already
# on PATH: the caller's overrides first, then the defaults. The incoming PATH always wins.
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
  done < <([ "$#" -gt 0 ] && printf '%s\n' "$@"; sutando_launch_path_dirs "$repo")
  export PATH
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ -n "${1:-}" ] || { echo "usage: launch-path.sh <repo>" >&2; exit 2; }
  sutando_launch_path_dirs "$1"
fi

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
  local repo="$1" d kept="" added="" dropped=""
  shift
  # Drop only the defaults an earlier call (the dispatcher, a parent core) appended, so this
  # caller's overrides land ahead of them; overrides are never recorded, so never dropped.
  while IFS= read -r -d: d; do
    case ":${_SUTANDO_LAUNCH_PATH_ADDED:-}:" in
      *":$d:"*) dropped="${dropped:+$dropped:}$d"; continue ;;
    esac
    kept="${kept:+$kept:}$d"
  done <<< "${PATH:+$PATH:}"
  PATH="$kept"
  for d in "$@"; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    d="$(cd "$d" && pwd)"
    case ":$PATH:" in *":$d:"*) ;; *) PATH="${PATH:+$PATH:}$d" ;; esac
  done
  # A dropped default this repo does not name (startup's app-node-dir) still goes back, last.
  while IFS= read -r d; do
    [ -n "$d" ] && [ -d "$d" ] || continue
    d="$(cd "$d" && pwd)"
    case ":$PATH:" in
      *":$d:"*) ;;
      *) PATH="${PATH:+$PATH:}$d"; added="${added:+$added:}$d" ;;
    esac
  done < <(sutando_launch_path_dirs "$repo"; [ -n "$dropped" ] && printf '%s\n' "${dropped//:/$'\n'}")
  export PATH _SUTANDO_LAUNCH_PATH_ADDED="$added"
}

# sutando_prepend_default_dir <dir>: put a node dir first for this script's own children
# (startup's services) but record it as a default, so a launcher still puts overrides ahead.
sutando_prepend_default_dir() {
  local d="$1"
  [ -n "$d" ] && [ -d "$d" ] || return 0
  case ":$PATH:" in *":$d:"*) return 0 ;; esac
  PATH="$d${PATH:+:$PATH}"
  export PATH _SUTANDO_LAUNCH_PATH_ADDED="${_SUTANDO_LAUNCH_PATH_ADDED:+$_SUTANDO_LAUNCH_PATH_ADDED:}$d"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  [ -n "${1:-}" ] || { echo "usage: launch-path.sh <repo>" >&2; exit 2; }
  sutando_launch_path_dirs "$1"
fi

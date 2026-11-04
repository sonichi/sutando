#!/bin/bash
# Launch-PATH policy shared by the runtime launchers and health-check's restart env:
# the engine-owned directories a core launch needs that a launchd job's PATH lacks.

# Sourced, defines the functions below; executed with <repo>, prints the directories.

# sutando_bundled_node_dir <repo>: where the desktop app ships node.
sutando_bundled_node_dir() {
  printf '%s\n' "$1/../runtime/node/bin"
}

# sutando_desktop_app_support <repo>: the desktop root: $SUTANDO_APP_SUPPORT, else the parent of
# <repo>'s engine/ dir. Prints nothing outside a desktop install or when the dir is missing.
# Pure Bash: early boot calls this before PATH is known to hold dirname/basename.
sutando_desktop_app_support() {
  local d="${SUTANDO_APP_SUPPORT:-}" r="$1" parent
  while [ "${#r}" -gt 1 ] && [ "${r%/}" != "$r" ]; do r="${r%/}"; done
  if [ -z "$d" ] && [ "${r#*/}" != "$r" ]; then
    parent="${r%/*}"
    [ "${parent##*/}" = engine ] && d="${parent%/*}"
  fi
  [ -n "$d" ] && [ -d "$d" ] && printf '%s\n' "$d"
  return 0
}

# sutando_launch_path_dirs <repo>: default dirs, one per line, existing or not.
# runtime/bin is the older bundle layout; <app support>/cli/*/bin is where the desktop app
# ships `claude` and `codex` (the only copy on a desktop install); ~/.local/bin is the CLI install.
sutando_launch_path_dirs() {
  local app
  sutando_bundled_node_dir "$1"
  printf '%s\n' "$1/../runtime/bin"
  app="$(sutando_desktop_app_support "$1")"
  [ -n "$app" ] && printf '%s\n' "$app/engine/runtime/node/bin" "$app/cli/claude/bin" \
    "$app/cli/codex/bin"
  printf '%s\n' "$HOME/.local/bin"
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

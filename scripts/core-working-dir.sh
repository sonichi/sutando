#!/usr/bin/env bash
# core-working-dir.sh — the ONE resolver for SUTANDO_CLAUDE_WORKING_DIR, the directory the
# Claude core launches from and therefore where Claude Code reads project .claude/settings.json.
#
# Contract (mirrored by health-check.py's _hook_settings_target, which cannot source bash):
#   unset / empty  -> the caller's default (the repo), printed unchanged
#   /absolute/path -> created if missing, printed as its physical path (cd … && pwd -P)
#   ~/relative     -> $HOME/relative, same treatment
#   anything else  -> REFUSED (return 1, message on stderr): `~user/…` is not expanded — the
#                     `${v/#\~/$HOME}` idiom this replaces turned it into $HOME + "user/…" and
#                     mkdir -p then created that — and a relative path has no stable meaning.
#
# Every site that decides or targets the launch dir sources this and calls the function:
# the launcher (src/agent/claude/cli/start-cli.sh) and the three settings installers. Four
# private copies of the expansion disagreed on `~user`; one owner cannot.
#
# Usage:
#   . "$REPO/scripts/core-working-dir.sh"
#   TARGET_DIR="$(sutando_core_working_dir "$REPO")" || exit 1

sutando_core_working_dir() {
  _scwd_default="${1:-}"
  _scwd_v="${SUTANDO_CLAUDE_WORKING_DIR:-}"
  if [ -z "$_scwd_v" ]; then
    printf '%s' "$_scwd_default"
    return 0
  fi
  case "$_scwd_v" in
    /*) _scwd_e="$_scwd_v" ;;
    "~/"*) _scwd_e="$HOME/${_scwd_v#\~/}" ;;
    *)
      echo "SUTANDO_CLAUDE_WORKING_DIR must be an absolute path or start with ~/ (got: $_scwd_v)" >&2
      return 1
      ;;
  esac
  mkdir -p "$_scwd_e" || { echo "can't create core working dir: $_scwd_e" >&2; return 1; }
  (cd "$_scwd_e" && pwd -P)
}

#!/usr/bin/env bash
# Sourced by launch-worker-session.sh. worker_claude_folder REPO PYTHON CORE_FOLDER WORKER prints the
# Claude folder to start WORKER in. Two cases are kept apart on purpose:
#   not configured (no <core folder>-workers/<worker>): the core's folder, as before;
#   configured, but the refresh failed (or no Python to run it): nothing printed, returns 1 with
#   what failed and how to fix it, so the launcher refuses rather than start it on the core's account.
worker_claude_folder() {
  local repo="$1" py="$2" core="$3" worker="$4" own err out why
  own="${core%/}-workers/$worker"
  if [ -n "$py" ]; then
    err="$(mktemp)" || err=/dev/null
    if out="$("$py" "$repo/skills/worker-pool/scripts/worker_claude_folder.py" launch "$core" "$worker" 2>"$err")" \
        && [ -n "$out" ] && [ -d "$out" ]; then
      [ "$err" = /dev/null ] || rm -f "$err"
      printf '%s\n' "$out"
      return 0
    fi
    why="$(head -n 1 "$err" 2>/dev/null)"
    [ "$err" = /dev/null ] || rm -f "$err"
  else
    why="no Python to refresh it"
  fi
  if [ ! -e "$own" ] && [ ! -L "$own" ]; then
    [ -n "$py" ] && echo "  ⚠ worker Claude folder helper failed (${why:-no reason given}); this worker has no folder of its own, so it starts in the core's" >&2
    printf '%s\n' "$core"
    return 0
  fi
  echo "  ✗ worker $worker has its own Claude folder ($own) but it could not be made ready: ${why:-the helper printed no folder}." >&2
  echo "    Not starting it in the core's folder, which would run it on the core's account." >&2
  echo "    Fix the cause above, then start the worker again; or remove $own to run this worker on the core's account." >&2
  return 1
}

#!/usr/bin/env bash
# Sourced by launch-worker-session.sh. worker_claude_folder REPO PYTHON CORE_FOLDER WORKER prints the
# Claude folder to start WORKER in. Two cases are kept apart on purpose:
#   not configured (<core folder>-workers/<worker> confirmed absent): the core's folder, as before;
#   configured, but the refresh failed (or no Python to run it): nothing printed, returns 1 with
#   what failed and how to fix it, so the launcher refuses rather than start it on the core's account.
# True only when PATH is known not to exist: a failed test alone may be a permission or I/O error, so
# the directory holding it (or, if that is missing too, the one above) must be searchable to trust it.
_worker_folder_confirmed_missing() {
  local path="$1" parent grand
  [ -e "$path" ] || [ -L "$path" ] && return 1
  parent="$(dirname "$path")"
  if [ -d "$parent" ] && [ -x "$parent" ]; then return 0; fi
  [ -e "$parent" ] || [ -L "$parent" ] && return 1
  grand="$(dirname "$parent")"
  [ -d "$grand" ] && [ -x "$grand" ]
}

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
  if _worker_folder_confirmed_missing "$own"; then
    [ -n "$py" ] && echo "  ⚠ worker Claude folder helper failed (${why:-no reason given}); this worker has no folder of its own, so it starts in the core's" >&2
    printf '%s\n' "$core"
    return 0
  fi
  echo "  ✗ worker $worker has its own Claude folder ($own) but it could not be made ready: ${why:-the helper printed no folder}." >&2
  echo "    Not starting it in the core's folder, which would run it on the core's account." >&2
  echo "    Fix the cause above, then start the worker again; or remove $own to run this worker on the core's account." >&2
  return 1
}

#!/bin/bash
# StopFailure hook: an API error ended the turn, so the prompt it was running is lost.
# Records it for the task notifier (src/delivery/turn_failure.py); fails open and silent.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! PYBIN="$(bash "$REPO_DIR/scripts/sutando-config.sh" python-bin 2>/dev/null)" \
   || [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  exit 0
fi
# The notifier's own workspace root when the launcher assigned one (a pool worker).
WORKSPACE="${SUTANDO_WORKSPACE_DIR:-$(bash "$REPO_DIR/scripts/sutando-config.sh" workspace 2>/dev/null)}"
[ -n "$WORKSPACE" ] || exit 0
"$PYBIN" "$REPO_DIR/src/delivery/turn_failure.py" hook-stop-failure --state "$WORKSPACE/state" >/dev/null 2>&1
exit 0

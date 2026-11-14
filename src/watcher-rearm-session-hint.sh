#!/usr/bin/env bash
# SessionStart(compact|resume) hook: when no ready session-role watcher holds
# this session's inbox, inject the exact command that re-arms it.

set -uo pipefail

# Only the marked core or an enrolled pool worker owes a watcher.
if [ "${SUTANDO_CORE_SESSION:-}" != "1" ] && [ -z "${SUTANDO_INSTANCE_ID:-}" ]; then
  exit 0
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="$(bash "$REPO_DIR/scripts/sutando-config.sh" workspace 2>/dev/null)"
[ -n "$WORKSPACE" ] || WORKSPACE="$REPO_DIR/workspace"
PYBIN="$(bash "$REPO_DIR/scripts/sutando-config.sh" python-bin 2>/dev/null)" || exit 0
[ -n "$PYBIN" ] && [ -x "$PYBIN" ] || exit 0

"$PYBIN" "$REPO_DIR/src/watcher_rearm.py" session-start --repo "$REPO_DIR" --workspace "$WORKSPACE" 2>/dev/null || true
exit 0

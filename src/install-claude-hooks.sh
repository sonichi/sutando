#!/bin/bash
# Sweeps out the hook entries earlier versions of this script wrote; it installs nothing.
# Name kept so existing callers still clean up. Hooks register at launch: build-core-settings.mjs.

# Usage: bash src/install-claude-hooks.sh [--dry-run]
# Exit: 0 swept (or nothing to sweep), 1 a settings file was unreadable, 2 no python.

set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck disable=SC1091
. "$REPO_DIR/scripts/python-binary.sh"
PY="$(resolve_python "$REPO_DIR")"
if [ -z "$PY" ]; then
  echo "install-claude-hooks: no runnable python3 — project hook sweep skipped" >&2
  exit 2
fi

exec "$PY" "$REPO_DIR/src/claude_hooks_settings.py" sweep --repo "$REPO_DIR" "$@"

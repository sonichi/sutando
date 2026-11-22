#!/bin/bash
# Hook for Claude Code and Codex: record whether the core is blocked on a dialog
# (src/core_block_signal.py). Fails open and prints nothing: stdout is hook output.
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -r "$REPO_DIR/scripts/python-binary.sh" ] || exit 0
. "$REPO_DIR/scripts/python-binary.sh" 2>/dev/null || exit 0
PYBIN="$(resolve_python "$REPO_DIR" 2>/dev/null)"
[ -n "$PYBIN" ] && [ -x "$PYBIN" ] || exit 0
"$PYBIN" "$REPO_DIR/src/core_block_signal.py" hook --runtime "${1:-claude}" >/dev/null 2>&1
exit 0

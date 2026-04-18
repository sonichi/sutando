#!/bin/bash
# Start the Sutando Claude Code CLI inside a named tmux session so that
# Sutando.app can send keystrokes to it when the task watcher dies.
#
# Usage: bash scripts/start-cli.sh
# Attach later: tmux attach -t sutando-core
#
# The session name `sutando-core` matches the literal used by
# Sutando.app's checkWatcher() tmux send-keys path.

set -e

SESSION="sutando-core"

# Check tmux is installed
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required. Install via: brew install tmux"
  exit 1
fi

# -A = attach if the session already exists, create otherwise.
# -s = session name.
# The trailing command string is what runs inside the pane.
exec tmux new-session -A -s "$SESSION" 'claude'

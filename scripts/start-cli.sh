#!/bin/bash
# scripts/start-cli.sh — canonical launch script for the sutando-core tmux
# session. Single source of truth for the "how to start Claude Code" command,
# so startup.sh + Sutando.app's Restart Core menu can both invoke it without
# duplicating the launch arguments.
#
# Usage:
#   bash scripts/start-cli.sh           # start (or attach if running)
#   bash scripts/start-cli.sh --restart # kill existing session then start fresh
#
# Per Chi's prompt 2026-05-05 ("shall we add core CLI-related commands in
# sutando app"): extracting the launch command from startup.sh's inline tmux
# block lets the menu-bar app's Restart Core action invoke the same canonical
# entry without re-implementing the tmux flags.

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

TMUX_SOCKET="/tmp/sutando-tmux.sock"
SESSION="sutando-core"

# --restart: kill any existing session before starting fresh. Without this,
# the script's "already running → attach" path returns and the old session
# keeps running.
#
# HAZARD: --restart MUST NOT be invoked from inside the sutando-core
# session itself — kill-session terminates the running agent mid-task.
# Safe callers: Sutando.app menu, terminal one-off, future health-check
# emit-task. Unsafe: a future agent processing a "restart core" task by
# exec'ing this script from within sutando-core. Per Mini's #608 review.
if [ "$1" = "--restart" ]; then
  if pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1; then
    echo "Killing existing $SESSION session..."
    tmux -S "$TMUX_SOCKET" kill-session -t "$SESSION" 2>/dev/null || true
    # Poll for actual shutdown — robust on slow machines, faster on fast
    # ones (~1s ceiling) than a fixed sleep.
    for _ in 1 2 3 4 5; do
      pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1 || break
      sleep 0.2
    done
  fi
fi

# Already running — attach if interactive, else exit cleanly. This branch
# also catches the !--restart path so re-running the script is idempotent.
if pgrep -f "claude.*--name.*$SESSION" > /dev/null 2>&1; then
  if [ -t 1 ] && command -v tmux > /dev/null 2>&1; then
    echo "Attaching to existing $SESSION (Ctrl-b d to detach)..."
    exec tmux -S "$TMUX_SOCKET" attach -t "$SESSION"
  fi
  echo "$SESSION already running."
  echo "To attach: tmux -S $TMUX_SOCKET attach -t $SESSION"
  exit 0
fi

# Auto-install tmux via Homebrew if missing. Sutando.app's
# watcher-auto-restart depends on a tmux-wrapped CLI pane.
if ! command -v tmux > /dev/null 2>&1 && command -v brew > /dev/null 2>&1; then
  echo "tmux not found — installing via Homebrew (~30s, required for Sutando.app watcher-auto-restart)..."
  brew install tmux 2>&1 | tail -3
fi

# Fall back to a bare `exec claude` if tmux is still missing.
if ! command -v tmux > /dev/null 2>&1; then
  echo "  ⚠ tmux not found — running without tmux wrapper"
  echo "    (Sutando.app's watcher-auto-restart won't work; brew install tmux to enable)"
  exec claude --name "$SESSION" --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    -- "/proactive-loop"
fi

# Explicit -S socket path so Sutando.app (which runs under a different
# TMPDIR due to macOS sandboxing when launched via `open`) can reach the
# same tmux server as the user shell (per #PR_444 watcher-auto-restart).
#
# Branch on whether we have a TTY:
#   - TTY (user running from terminal): exec attach so the user sees the
#     Claude Code prompt and the script process IS the tmux client.
#   - No TTY (Sutando.app's Restart Core or any background invocation):
#     start detached so we don't hang, server keeps running.
if [ -t 1 ]; then
  exec tmux -S "$TMUX_SOCKET" new-session -A -s "$SESSION" \
    claude --name "$SESSION" --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    -- "/proactive-loop"
else
  tmux -S "$TMUX_SOCKET" new-session -d -s "$SESSION" \
    claude --name "$SESSION" --remote-control "Sutando" --dangerously-skip-permissions --add-dir "$HOME" \
    -- "/proactive-loop"
  echo "Started $SESSION detached. Attach via Open Core CLI in menu bar, or:"
  echo "  tmux -S $TMUX_SOCKET attach -t $SESSION"
fi

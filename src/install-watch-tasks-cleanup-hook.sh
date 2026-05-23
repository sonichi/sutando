#!/bin/bash
# install-watch-tasks-cleanup-hook.sh — idempotent install of the Stop hook
# that pairs with src/watch-tasks-stream.sh (orphan-watcher cleanup).
#
# Inserts the watcher-cleanup command into the project-level Claude Code
# settings.json (~/Desktop/sutando/.claude/settings.json, gitignored — per
# `feedback_claude_code_hook_scoping`, sutando hooks belong project-level
# so they only fire when Claude runs in this project context).
#
# Pairs with the change in #1065 — watch-tasks-stream.sh now writes its PID
# to <workspace>/state/watch-tasks-stream.pid on startup; this Stop hook
# kills that PID + removes the file when the session ends, closing the
# orphan-watcher gap diagnosed in #1061 / #1063 (PARTIAL/FAIL across two
# Macs). Without this hook the watcher survives session-compaction /
# `/pull-and-restart` / ⌘Q exit and silently swallows DM events for hours.
#
# Idempotent: re-running is safe. Existing hook entries with the same
# command string are detected and not re-added.
#
# Why a standalone installer (not a fleet `install-hooks.sh`):
# per Chi 2026-05-23, fleet installer is deferred (option 3 from the A/B/C
# discussion). Each PR ships its own targeted installer for the hook(s)
# it adds — mirrors Lucy's pattern in #1056 (`install-hook.sh` installing
# the SessionStop → session-handoff.sh entry). Future fresh-Mac onboarding
# may consolidate.
#
# Usage:
#   bash src/install-watch-tasks-cleanup-hook.sh
#
# Exit codes:
#   0 — installed (new) OR already present (no-op)
#   1 — settings.json malformed / write failed
#   2 — jq missing (required for atomic edit)

set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$REPO_DIR/.claude/settings.json"

CMD='PID_FILE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/state/watch-tasks-stream.pid"; if [ -f "$PID_FILE" ]; then PID=$(cat "$PID_FILE" 2>/dev/null); [ -n "$PID" ] && kill "$PID" 2>/dev/null; rm -f "$PID_FILE"; fi; exit 0'

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required for atomic settings.json edit" >&2
  exit 2
fi

mkdir -p "$REPO_DIR/.claude"
if [ ! -f "$SETTINGS" ]; then
  echo '{}' > "$SETTINGS"
fi

# Detect existing entry by command-string match.
if jq -e --arg cmd "$CMD" \
    '.hooks.Stop // [] | map(.hooks // []) | flatten | map(.command) | index($cmd)' \
    "$SETTINGS" >/dev/null 2>&1; then
  echo "already installed (no change to $SETTINGS)"
  exit 0
fi

# Insert into hooks.Stop[0].hooks (creating intermediate keys if absent).
# Atomic write: edit to tmp, rename.
TMP="$(mktemp "${SETTINGS}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

jq --arg cmd "$CMD" '
  .hooks //= {}
  | .hooks.Stop //= [{"matcher": "", "hooks": []}]
  | (.hooks.Stop[0].hooks //= [])
  | .hooks.Stop[0].hooks += [{"type": "command", "command": $cmd}]
' "$SETTINGS" > "$TMP" || { echo "error: jq edit failed" >&2; exit 1; }

mv "$TMP" "$SETTINGS"
trap - EXIT

echo "installed watcher-cleanup Stop hook → $SETTINGS"

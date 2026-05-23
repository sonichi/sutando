#!/bin/bash
# install-claude-hooks.sh — idempotent install of Sutando-owned project-level
# Claude Code hooks (PreCompact + Stop). Pairs with the in-#1065 PID-file
# Stop-hook cleanup; also covers the previously-uninstaller'd entries that
# existed manually on dev machines.
#
# Per `feedback_claude_code_hook_scoping`: sutando hooks belong at PROJECT-level
# `.claude/settings.json` (gitignored, per-machine), NOT user-level
# `~/.claude/settings.json` — they only fire when Claude runs in this project
# context, not in unrelated sessions.
#
# Hooks installed (4):
#   PreCompact  → cp $TRANSCRIPT_PATH ~/Desktop/sutando-conversations/...
#   PreCompact  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   Stop        → bash src/check-pending-tasks.sh
#   Stop        → watcher-cleanup PID kill (the #1065 fix for #1061 / #1063)
#
# Note: Lucy's #1056 ships a SEPARATE installer for the SessionStop hook
# (skills/catchup-after-startup/scripts/install-hook.sh).  Those events
# don't overlap with this script's PreCompact + Stop entries, so the two
# installers are independent.  Run both on a fresh Mac.
#
# Idempotent: re-running is safe.  Existing hook entries with the same
# command string are detected per-hook and not re-added.  jq + tmp+mv for
# atomic write.
#
# Usage:
#   bash src/install-claude-hooks.sh
#
# Exit codes:
#   0 — all 4 hooks present after run (some may have been pre-existing)
#   1 — settings.json malformed / jq edit failed
#   2 — jq missing (required for atomic edit)

set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$REPO_DIR/.claude/settings.json"

# Hook specs: each line is "<event>|<command>".  Order = install order.
HOOKS=(
  "PreCompact|cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\""
  "PreCompact|bash \$HOME/Desktop/sutando/src/session-handoff.sh \"\$TRANSCRIPT_PATH\""
  "Stop|bash \$HOME/Desktop/sutando/src/check-pending-tasks.sh"
  "Stop|PID_FILE=\"\${SUTANDO_WORKSPACE:-\$HOME/.sutando/workspace}/state/watch-tasks-stream.pid\"; if [ -f \"\$PID_FILE\" ]; then PID=\$(cat \"\$PID_FILE\" 2>/dev/null); [ -n \"\$PID\" ] && kill \"\$PID\" 2>/dev/null; rm -f \"\$PID_FILE\"; fi; exit 0"
)

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required for atomic settings.json edit" >&2
  exit 2
fi

mkdir -p "$REPO_DIR/.claude"
if [ ! -f "$SETTINGS" ]; then
  echo '{}' > "$SETTINGS"
fi

ADDED=0
SKIPPED=0

for entry in "${HOOKS[@]}"; do
  EVENT="${entry%%|*}"
  CMD="${entry#*|}"

  # Detect existing entry by command-string match within this event's hooks list.
  if jq -e --arg event "$EVENT" --arg cmd "$CMD" \
      '(.hooks // {})[$event] // [] | map(.hooks // []) | flatten | map(.command) | index($cmd)' \
      "$SETTINGS" >/dev/null 2>&1; then
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  TMP="$(mktemp "${SETTINGS}.XXXXXX")"
  jq --arg event "$EVENT" --arg cmd "$CMD" '
    .hooks //= {}
    | .hooks[$event] //= [{"matcher": "", "hooks": []}]
    | (.hooks[$event][0].hooks //= [])
    | .hooks[$event][0].hooks += [{"type": "command", "command": $cmd}]
  ' "$SETTINGS" > "$TMP" || { echo "error: jq edit failed on $EVENT" >&2; rm -f "$TMP"; exit 1; }
  mv "$TMP" "$SETTINGS"
  ADDED=$((ADDED + 1))
done

echo "install-claude-hooks: added=$ADDED skipped=$SKIPPED → $SETTINGS"

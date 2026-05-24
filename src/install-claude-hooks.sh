#!/bin/bash
# install-claude-hooks.sh — idempotent install of Sutando-owned project-level
# Claude Code hooks (PreCompact + Stop).
#
# Per `feedback_claude_code_hook_scoping`: sutando hooks belong at PROJECT-level
# `.claude/settings.json` (gitignored, per-machine), NOT user-level
# `~/.claude/settings.json` — they only fire when Claude runs in this project
# context, not in unrelated sessions.
#
# Hooks installed (3):
#   PreCompact  → cp $TRANSCRIPT_PATH ~/Desktop/sutando-conversations/...
#   PreCompact  → bash src/session-handoff.sh "$TRANSCRIPT_PATH"
#   Stop        → bash src/check-pending-tasks.sh
#
# Historical note: a 4th hook (`Stop` → watcher-cleanup PID kill, the #1065
# fix) was removed 2026-05-24.  Claude Code's `Stop` event fires on
# turn-end (after every assistant response), NOT session-end — so the
# PID-kill block killed the live Monitor watcher every turn, triggering an
# exit-143 + Monitor-restart cycle.  Watcher orphan-cleanup is handled by
# the `Reap any stale watch-tasks-stream watcher` block in
# `src/startup.sh` (defense-in-depth: PID-file + cmdline-check before
# kill), which runs at every session start.  See the original #1061 /
# #1063 / #1065 thread for the orphan-watcher background.
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
#   0 — all 3 hooks present after run (some may have been pre-existing)
#   1 — settings.json malformed / jq edit failed
#   2 — jq missing (required for atomic edit)
#
# Existing installs: this script is install-only, not uninstall.  If your
# settings.json already has the watcher-kill Stop hook from a previous
# install, remove it manually:
#   jq 'del(.hooks.Stop[0].hooks[] | select(.command | contains("watch-tasks-stream.pid")))' \
#     .claude/settings.json > /tmp/s.json && mv /tmp/s.json .claude/settings.json

set -u

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$REPO_DIR/.claude/settings.json"

# Hook specs: each line is "<event>|<command>".  Order = install order.
HOOKS=(
  "PreCompact|cp \"\$TRANSCRIPT_PATH\" \"\$HOME/Desktop/sutando-conversations/\$(date +%Y-%m-%dT%H-%M-%S).jsonl\""
  "PreCompact|bash \$HOME/Desktop/sutando/src/session-handoff.sh \"\$TRANSCRIPT_PATH\""
  "Stop|bash \$HOME/Desktop/sutando/src/check-pending-tasks.sh"
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

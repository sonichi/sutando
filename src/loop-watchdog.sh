#!/bin/bash
# loop-watchdog.sh — read-only durability watchdog
#
# Run by launchd or cron every ~15 min. Checks whether the proactive-loop is
# still firing by inspecting <workspace>/state/core-status.json mtime. Logs
# a warning + appends a pending question if stale.
#
# Does NOT auto-spawn Claude. Recovery is still manual (user runs /proactive-loop).
# This script's job is observability — turning silent death into a visible signal.
#
# Install (manual): see src/install-loop-watchdog.sh for the launchd path.
# Or via crontab:
#   */15 * * * * bash $SUTANDO_REPO_DIR/src/loop-watchdog.sh

set -euo pipefail

# Workspace resolution: SUTANDO_HOME (set by the app bundle to
# ~/Library/Application Support/Sutando) wins. Falls back to
# SUTANDO_WORKSPACE, then the default ~/.sutando/workspace.
# core-status.json always lives under <workspace>/state/.
if [ -n "${SUTANDO_HOME:-}" ]; then
  WORKSPACE="${SUTANDO_HOME}"
else
  WORKSPACE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
fi
WORKSPACE="${WORKSPACE/#\~/$HOME}"  # expand ~ if env value used it literally

STATUS_FILE="$WORKSPACE/state/core-status.json"
LOG_FILE="$WORKSPACE/logs/watchdog.log"
PENDING_FILE="$WORKSPACE/pending-questions.md"
STALE_THRESHOLD_SEC=900   # 15 min

mkdir -p "$WORKSPACE/logs"

NOW=$(date +%s)
ISO=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

if [ ! -f "$STATUS_FILE" ]; then
  echo "$ISO  WARN  $STATUS_FILE missing — proactive-loop has never run on this machine" >> "$LOG_FILE"
  exit 0
fi

MTIME=$(stat -f "%m" "$STATUS_FILE")
AGE=$((NOW - MTIME))

if [ "$AGE" -lt "$STALE_THRESHOLD_SEC" ]; then
  # Healthy — quiet success
  echo "$ISO  OK    age=${AGE}s" >> "$LOG_FILE"
  exit 0
fi

# Stale — log + emit a pending question (idempotent: only add if not already present)
AGE_MIN=$((AGE / 60))
echo "$ISO  STALE age=${AGE_MIN}min — proactive-loop appears dead" >> "$LOG_FILE"

if ! grep -q "## ⚠️ Proactive-loop stale" "$PENDING_FILE" 2>/dev/null; then
  cat >> "$PENDING_FILE" << EOF

## ⚠️ Proactive-loop stale — possible session death
- **Detected:** $ISO
- **core-status.json age:** ${AGE_MIN} min (threshold: $((STALE_THRESHOLD_SEC / 60)) min)
- **Likely cause:** Claude session ended; CronCreate jobs are session-only and didn't survive.
- **Recovery:** open Claude in the Sutando workspace and run \`/proactive-loop\`.
- **Long-term fix:** see notes/durable-cron-options-2026-05-05.md.
EOF
fi

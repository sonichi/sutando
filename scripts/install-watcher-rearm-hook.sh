#!/usr/bin/env bash
# Idempotently registers src/watcher-rearm-session-hint.sh under SessionStart
# "compact|resume"; target dir and merge mirror install-personal-claude-hook.sh.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck disable=SC1091
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"
if [ -z "$PY" ]; then
  echo "  ⚠ no runnable python3 — skipping watcher re-arm hook install" >&2
  exit 0
fi

# shellcheck disable=SC1091
. "$REPO/scripts/core-working-dir.sh"
TARGET_DIR="$(sutando_core_working_dir "$REPO")" || { echo "  ✗ SUTANDO_CLAUDE_WORKING_DIR rejected — hook not installed" >&2; exit 1; }

SETTINGS="$TARGET_DIR/.claude/settings.json"
HOOK_CMD="bash \"$REPO/src/watcher-rearm-session-hint.sh\""

mkdir -p "$TARGET_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{"hooks":{}}' > "$SETTINGS"

"$PY" "$REPO/src/claude_hooks_settings.py" install --settings "$SETTINGS" \
  --event SessionStart --command "$HOOK_CMD" --matcher "compact|resume" \
  --label "watcher re-arm SessionStart hook"

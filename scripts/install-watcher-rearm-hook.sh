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

if [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ]; then
  _cwd_exp="${SUTANDO_CLAUDE_WORKING_DIR/#\~/$HOME}"
  mkdir -p "$_cwd_exp" || { echo "  ✗ can't create core working dir: $_cwd_exp" >&2; exit 1; }
  TARGET_DIR="$(cd "$_cwd_exp" && pwd -P)"
else
  TARGET_DIR="$REPO"
fi

SETTINGS="$TARGET_DIR/.claude/settings.json"
HOOK_CMD="bash \"$REPO/src/watcher-rearm-session-hint.sh\""

mkdir -p "$TARGET_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{"hooks":{}}' > "$SETTINGS"

"$PY" "$REPO/src/claude_hooks_settings.py" install --settings "$SETTINGS" \
  --event SessionStart --command "$HOOK_CMD" --matcher "compact|resume" \
  --label "watcher re-arm SessionStart hook"

#!/usr/bin/env bash
# Idempotently wire the PERSONAL_CLAUDE.md compaction re-inject hook into the
# project-level .claude/settings.json. Called by startup.sh and safe to run
# standalone. Mirrors scripts/install-session-start-hook.sh — same target-dir
# resolution, same idempotent merge — but registers under the SessionStart
# "compact" matcher only: startup/resume are covered by the session-start Read
# (CLAUDE.md "Personal overrides"); compaction is the gap this hook closes.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# shellcheck disable=SC1091
. "$REPO/scripts/python-binary.sh"
PY="$(resolve_python "$REPO")"
if [ -z "$PY" ]; then
  echo "  ⚠ no runnable python3 — skipping PERSONAL_CLAUDE compact-reinject hook install" >&2
  exit 0
fi

# Target the directory the core `claude` process actually launches from — that
# is where Claude Code reads project-scoped `.claude/settings.json`. Same
# resolution as install-session-start-hook.sh: SUTANDO_CLAUDE_WORKING_DIR when
# set (expanded + physically resolved the way start-cli.sh does), else $REPO.
# shellcheck disable=SC1091
. "$REPO/scripts/core-working-dir.sh"
TARGET_DIR="$(sutando_core_working_dir "$REPO")" || { echo "  ✗ SUTANDO_CLAUDE_WORKING_DIR rejected — hook not installed" >&2; exit 1; }

SETTINGS="$TARGET_DIR/.claude/settings.json"
# The hint script itself always lives in this checkout ($REPO) — the working
# dir may be a different tree, but this is the checkout that ran startup.sh and
# is known to contain the script.
HINT_SCRIPT="$REPO/src/personal-claude-compact-hint.sh"
HOOK_CMD="bash \"$HINT_SCRIPT\""

mkdir -p "$TARGET_DIR/.claude"

if [ ! -f "$SETTINGS" ]; then
  echo '{"hooks":{}}' > "$SETTINGS"
fi

# One merge for every Sutando hook installer (src/claude_hooks_settings.py): adds this
# entry once and removes dead copies of the SAME hook — entries whose script no longer
# exists, e.g. left by a test run from a temp copy of the repo. Other hooks are untouched.
"$PY" "$REPO/src/claude_hooks_settings.py" install --settings "$SETTINGS" \
  --event SessionStart --command "$HOOK_CMD" --matcher compact \
  --label "PERSONAL_CLAUDE compact-reinject hook"

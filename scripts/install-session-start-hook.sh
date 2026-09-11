#!/usr/bin/env bash
# Idempotently wire the /schedule-crons SessionStart hook into the project-level
# .claude/settings.json. Called by startup.sh and safe to run standalone.
#
# The hook injects additionalContext reminding the core agent to run
# /schedule-crons at the start of every session (including post-compaction
# restarts), so all 16 session-only crons are always registered.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# Target the directory the core `claude` process actually launches from — that
# is where Claude Code reads project-scoped `.claude/settings.json`. The core
# launcher (src/agent/claude/cli/start-cli.sh) anchors the cwd to
# SUTANDO_CLAUDE_WORKING_DIR when set (e.g. Sutando.app exports
# $HOME/.sutando/repo, a stable path that survives app-bundle upgrades);
# otherwise it launches from $REPO. Mirror that resolution here — expand a
# leading ~ and resolve to the physical absolute path the SAME way start-cli.sh
# does (cd … && pwd -P), so the hook lands in the project Claude will actually
# read. Writing to $REPO/.claude/settings.json in the SUTANDO_CLAUDE_WORKING_DIR
# configuration installs the hook in the wrong project and it never fires.
if [ -n "${SUTANDO_CLAUDE_WORKING_DIR:-}" ]; then
  _cwd_exp="${SUTANDO_CLAUDE_WORKING_DIR/#\~/$HOME}"
  mkdir -p "$_cwd_exp" || { echo "  ✗ can't create core working dir: $_cwd_exp" >&2; exit 1; }
  TARGET_DIR="$(cd "$_cwd_exp" && pwd -P)"
else
  TARGET_DIR="$REPO"
fi

SETTINGS="$TARGET_DIR/.claude/settings.json"
# The hint script itself always lives in this checkout ($REPO) — the working
# dir may be a different tree, but this is the checkout that ran startup.sh and
# is known to contain the script.
HINT_SCRIPT="$REPO/src/schedule-crons-session-hint.sh"
HOOK_CMD="bash \"$HINT_SCRIPT\""

# Ensure the target .claude dir exists
mkdir -p "$TARGET_DIR/.claude"

# Create settings.json with empty hooks structure if missing
if [ ! -f "$SETTINGS" ]; then
  echo '{"hooks":{}}' > "$SETTINGS"
fi

# One merge for every Sutando hook installer (src/claude_hooks_settings.py): adds this
# entry once, first so it fires first, and removes dead copies of the same hook.
PY=""
if [ -r "$REPO/scripts/python-binary.sh" ]; then
  . "$REPO/scripts/python-binary.sh"
  PY="$(resolve_python "$REPO")"
fi
[ -n "$PY" ] && [ -x "$PY" ] || PY="python3"
"$PY" "$REPO/src/claude_hooks_settings.py" install --settings "$SETTINGS" \
  --event SessionStart --command "$HOOK_CMD" --matcher "" --prepend \
  --label "schedule-crons SessionStart hook"

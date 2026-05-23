#!/bin/bash
# install-loop-watchdog.sh — render + load the launchd plist for loop-watchdog
#
# Reads:
#   SUTANDO_REPO_DIR  — defaults to this checkout (computed from script path)
#   SUTANDO_HOME      — app bundle workspace (preferred for state/logs)
#   SUTANDO_WORKSPACE — fallback workspace (matches workspace_default.py)
#
# Renders src/com.sutando.loop-watchdog.plist.template → ~/Library/LaunchAgents/
# with both placeholders substituted, then `launchctl bootstrap`-loads it.
#
# Re-runnable. Existing service is unloaded before re-load so updates apply.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DEFAULT="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO="${SUTANDO_REPO_DIR:-$REPO_DEFAULT}"
# Workspace resolution: SUTANDO_HOME (app bundle) wins over SUTANDO_WORKSPACE,
# then the default ~/.sutando/workspace.
if [ -n "${SUTANDO_HOME:-}" ]; then
  WORKSPACE="${SUTANDO_HOME}"
else
  WORKSPACE="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}"
fi
# expand ~ in env-supplied values
REPO="${REPO/#\~/$HOME}"
WORKSPACE="${WORKSPACE/#\~/$HOME}"

TEMPLATE="$SCRIPT_DIR/com.sutando.loop-watchdog.plist.template"
DEST="$HOME/Library/LaunchAgents/com.sutando.loop-watchdog.plist"
LABEL="com.sutando.loop-watchdog"

if [ ! -f "$TEMPLATE" ]; then
  echo "ERR  template not found: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"
mkdir -p "$WORKSPACE/logs"

# Render. Using sed with `|` separator to avoid escaping path slashes.
sed -e "s|__SUTANDO_REPO__|$REPO|g" \
    -e "s|__SUTANDO_WORKSPACE__|$WORKSPACE|g" \
    "$TEMPLATE" > "$DEST"

echo "rendered: $DEST"
echo "  repo:      $REPO"
echo "  workspace: $WORKSPACE"

# Unload previous (if loaded). launchctl bootout is the modern replacement
# for `unload`; tolerate failure on first install when no service exists.
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$DEST"

echo "loaded: $LABEL"
echo
echo "Verify:"
echo "  launchctl list | grep $LABEL"
echo "  tail -f $WORKSPACE/logs/watchdog.log"

#!/bin/bash
# Post one macOS notification, only when the owner opted in (notifications.macos).
# Usage: bash src/macos-notify.sh MESSAGE [TITLE]. Exits 0 when off; 1 when osascript fails.
# Invoked as macos-notify-always.sh it skips the opt-in, for an alert with no DM to fall back on.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(basename "${BASH_SOURCE[0]}")" != "macos-notify-always.sh" ]; then
  [ "$(bash "$REPO/scripts/sutando-config.sh" macos-notifications 2>/dev/null)" = "true" ] || exit 0
fi
command -v osascript >/dev/null 2>&1 || exit 1
# Message and title travel as argv, never spliced into the script text.
exec osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
  -e 'end run' -- "${1:-}" "${2:-Sutando}" >/dev/null 2>&1

#!/bin/bash
# Post one macOS notification, only when the owner opted in (notifications.macos).
# Usage: bash src/macos-notify.sh MESSAGE [TITLE]. Exits 0 when off; 1 when osascript fails.
# src/macos-notify-always.sh skips the opt-in, for an alert with no DM to fall back on.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(bash "$REPO/scripts/sutando-config.sh" macos-notifications 2>/dev/null)" = "true" ] || exit 0
exec bash "$REPO/src/_macos-post.sh" "$@"

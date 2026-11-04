#!/bin/bash
# Post one macOS notification even with notifications.macos off. Only for an alert with no DM to fall back on.
# Usage: bash src/macos-notify-always.sh MESSAGE [TITLE]
SUTANDO_MACOS_NOTIFY_UNGATED=1 exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/macos-notify.sh" "$@"

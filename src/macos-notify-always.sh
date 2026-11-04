#!/bin/bash
# Post one macOS notification even with notifications.macos off. Only for an alert with no DM to fall back on.
# Usage: bash src/macos-notify-always.sh MESSAGE [TITLE]
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_macos-post.sh" "$@"

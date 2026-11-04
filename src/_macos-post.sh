#!/bin/bash
# Ungated osascript poster. Only macos-notify.sh (after the opt-in check) and macos-notify-always.sh may call it.
# Usage: bash src/_macos-post.sh MESSAGE [TITLE]. Exits 1 when osascript is missing or fails.
command -v osascript >/dev/null 2>&1 || exit 1
# Message and title travel as argv, never spliced into the script text.
exec osascript -e 'on run argv' -e 'display notification (item 1 of argv) with title (item 2 of argv)' \
  -e 'end run' -- "${1:-}" "${2:-Sutando}" >/dev/null 2>&1

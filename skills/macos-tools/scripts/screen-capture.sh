#!/bin/bash
# Capture the current screen and output the path.
# Usage: bash src/screen-capture.sh [region]
#   No args     → full screen
#   "region"    → interactive region select (user drags a rectangle)
#
# Output: path to the captured PNG file

# Per-user temp dir (same shape as browser.mjs / screen-capture-server.py):
# shared /tmp dir EACCES-fails the second account on multi-user Macs.
DIR="${SUTANDO_SCREENSHOT_DIR:-${TMPDIR:-/tmp}/sutando-screenshots}"
mkdir -p "$DIR"

TIMESTAMP=$(date '+%Y%m%d-%H%M%S')
FILE="$DIR/screen-$TIMESTAMP.png"

case "${1:-full}" in
  region)
    screencapture -i "$FILE" 2>/dev/null
    ;;
  *)
    screencapture -x "$FILE" 2>/dev/null
    ;;
esac

# Fallback: if screencapture failed (permissions), try via osascript
if [ ! -f "$FILE" ] || [ ! -s "$FILE" ]; then
  osascript -e "do shell script \"screencapture -x '$FILE'\"" 2>/dev/null
fi

if [ -f "$FILE" ] && [ -s "$FILE" ]; then
  echo "$FILE"
else
  echo "ERROR: Screen capture failed — grant Screen Recording permission in System Settings → Privacy & Security" >&2
  exit 1
fi

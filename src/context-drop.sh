#!/bin/bash
# Sutando context drop — triggered by macOS hotkey via Automator Quick Action.
#
# What it does:
#   1. Gets currently selected text OR image from clipboard
#   2. Writes text to context-drop.txt, images to context-drop-image.png
#   3. If a file is selected in Finder, captures its path
#   4. The cron loop picks it up next pass and processes it
#
# Setup:
#   1. Open Automator → New → Quick Action
#   2. Set "Workflow receives" = "no input" in "any application"
#   3. Add action: "Run Shell Script" → point to this file
#   4. Save as "Sutando: Drop Context"
#   5. System Settings → Keyboard → Keyboard Shortcuts → Services
#      → assign a shortcut

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
DROP_FILE="$WORKSPACE/context-drop.txt"
DROP_IMAGE="$WORKSPACE/tasks/image-$(date +%s%3N).png"
LOG_FILE="$WORKSPACE/src/context-drop.log"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# --- Check for Finder file selection FIRST (before clipboard) ---
# Always check Finder selection regardless of frontmost app
# (Automator may steal focus before script runs)
FINDER_FILE=$(osascript -e '
tell application "Finder"
  try
    set sel to selection
    if (count of sel) > 0 then
      return POSIX path of (item 1 of sel as alias)
    end if
  on error
    return ""
  end try
end tell
' 2>/dev/null)

if [ -n "$FINDER_FILE" ] && [ -e "$FINDER_FILE" ]; then
  cat > "$DROP_FILE" << EOF
timestamp: $TIMESTAMP
type: file
path: $FINDER_FILE
---
[File selected in Finder: $FINDER_FILE]
EOF
  echo "[$TIMESTAMP] Dropped: file ($FINDER_FILE)" >> "$LOG_FILE"
  BASENAME=$(basename "$FINDER_FILE")
  osascript -e "display notification \"File dropped: $BASENAME\" with title \"Sutando\""
  exit 0
fi

# --- Check for image in clipboard ---
HAS_IMAGE=$(osascript -e '
try
  set theClip to the clipboard as «class PNGf»
  return "yes"
on error
  return "no"
end try
' 2>/dev/null)

if [ "$HAS_IMAGE" = "yes" ]; then
  osascript -e '
  set theFile to POSIX file "'"$DROP_IMAGE"'"
  set theData to the clipboard as «class PNGf»
  set fileRef to open for access theFile with write permission
  set eof fileRef to 0
  write theData to fileRef
  close access fileRef
  ' 2>/dev/null

  if [ -f "$DROP_IMAGE" ]; then
    cat > "$DROP_FILE" << EOF
timestamp: $TIMESTAMP
type: image
path: $DROP_IMAGE
---
[Image dropped from clipboard]
EOF
    echo "[$TIMESTAMP] Dropped: image ($(wc -c < "$DROP_IMAGE") bytes)" >> "$LOG_FILE"
    osascript -e 'display notification "Image dropped — processing next pass" with title "Sutando"'
    exit 0
  fi
fi

# --- Fall back to text selection ---
OLD_CLIPBOARD=$(pbpaste 2>/dev/null)

# Method 1: Accessibility API (works in apps that block simulated keystrokes like Discord)
SELECTED=$(osascript -e '
tell application "System Events"
  try
    set frontApp to name of first application process whose frontmost is true
    tell process frontApp
      set selectedText to value of attribute "AXSelectedText" of (first text area whose focused is true)
      return selectedText
    end tell
  on error
    return ""
  end try
end tell
' 2>/dev/null)

# Method 2: Simulated Cmd+C (fallback for apps where AX doesn't work)
if [ -z "$SELECTED" ]; then
  osascript -e 'tell application "System Events" to keystroke "c" using command down'
  sleep 0.3
  SELECTED=$(pbpaste 2>/dev/null)
  # If clipboard didn't change, nothing was copied
  if [ "$SELECTED" = "$OLD_CLIPBOARD" ]; then
    SELECTED=""
  fi
fi

if [ -z "$SELECTED" ]; then
  echo "[$TIMESTAMP] Nothing selected" >> "$LOG_FILE"
  osascript -e 'display notification "Nothing selected — select text first" with title "Sutando"'
  exit 0
fi

# Write to drop file with timestamp
cat > "$DROP_FILE" << EOF
timestamp: $TIMESTAMP
type: text
---
$SELECTED
EOF

echo "[$TIMESTAMP] Dropped: ${#SELECTED} chars" >> "$LOG_FILE"

# Notify user
osascript -e "display notification \"${#SELECTED} chars dropped — processing next pass\" with title \"Sutando\""

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
LOG_FILE="$WORKSPACE/logs/context-drop.log"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# --- Check for Finder file selection FIRST (before clipboard) ---
# Always check Finder selection regardless of frontmost app
# (Automator may steal focus before script runs).
# Returns newline-separated POSIX paths for ALL selected items (not just first).
FINDER_FILES=$(osascript -e '
tell application "Finder"
  try
    set sel to selection
    set out to ""
    repeat with anItem in sel
      set out to out & POSIX path of (anItem as alias) & linefeed
    end repeat
    return out
  on error
    return ""
  end try
end tell
' 2>/dev/null)

# Filter to existing files; count valid ones.
VALID_FILES=()
while IFS= read -r f; do
  if [ -n "$f" ] && [ -e "$f" ]; then
    VALID_FILES+=("$f")
  fi
done <<< "$FINDER_FILES"

if [ ${#VALID_FILES[@]} -eq 1 ]; then
  FINDER_FILE="${VALID_FILES[0]}"
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
elif [ ${#VALID_FILES[@]} -gt 1 ]; then
  # Emit JSON-array on the `paths:` line so a path with spaces or colons
  # parses unambiguously (no YAML lib needed downstream).
  PATHS_JSON=$(printf '%s\n' "${VALID_FILES[@]}" | python3 -c 'import sys,json; print(json.dumps([l.rstrip("\n") for l in sys.stdin]))')
  HUMAN_LIST=""
  for f in "${VALID_FILES[@]}"; do
    HUMAN_LIST+="  - $f"$'\n'
  done
  HUMAN_LIST="${HUMAN_LIST%$'\n'}"
  cat > "$DROP_FILE" << EOF
timestamp: $TIMESTAMP
type: files
paths: $PATHS_JSON
---
[Files selected in Finder: ${#VALID_FILES[@]} files]
$HUMAN_LIST
EOF
  echo "[$TIMESTAMP] Dropped: ${#VALID_FILES[@]} files" >> "$LOG_FILE"
  osascript -e "display notification \"${#VALID_FILES[@]} files dropped\" with title \"Sutando\""
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

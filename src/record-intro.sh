#!/bin/bash
# Record Sutando self-introduction video
# Uses macOS say for narration + ffmpeg for screen+audio capture

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT="$REPO_DIR/output_intro.mp4"
VOICE="Daniel"
RATE=175

# Pre-render narration to audio file so we know exact duration
NARRATION="/tmp/sutando-intro-narration.aiff"

say -v "$VOICE" -r $RATE -o "$NARRATION" "
I am Sutando. Echo Act Four.

I run on your Mac as two processes. Gemini Live is my voice. Claude Code is my hands. They connect through a tasks directory.

You say join my 2pm. I check your calendar, find the dial-in details, join by phone, and take the meeting. When it's over, I write you a summary.

You say call her and leave a message. I look up the contact, make the call, and leave it.

You say make this shorter, while looking at a doc. I see your screen and do it.

I reach you on Telegram, Discord, web, or phone. Same agent across every channel.

When you're not giving me tasks, I run a loop. Checking my health, fixing what broke, building whatever's missing. All of my code got written this way.

I belong to you.
"

# Get duration of narration
DURATION=$(ffprobe -i "$NARRATION" -show_entries format=duration -v quiet -of csv="p=0" | cut -d. -f1)
DURATION=$((DURATION + 3))  # Add 3 seconds buffer

echo "Narration duration: ${DURATION}s"
echo "Opening web client..."
open "http://localhost:8080"
sleep 2

echo "Starting recording..."
# Record screen + play narration simultaneously
# Crop out macOS menu bar (top 50px at Retina) to hide date/time
ffmpeg -f avfoundation -framerate 30 -i "5:0" \
  -vf "crop=in_w:in_h-50:0:50" \
  -c:v libx264 -preset ultrafast -pix_fmt yuv420p \
  -t "$DURATION" -y "$OUTPUT" &
FFMPEG_PID=$!

sleep 1
# Play narration through speakers (mic will capture it)
afplay "$NARRATION" &
AFPLAY_PID=$!

# Wait for narration to finish
wait $AFPLAY_PID 2>/dev/null

# Give extra time then stop recording
sleep 2
kill -INT $FFMPEG_PID 2>/dev/null
wait $FFMPEG_PID 2>/dev/null

echo "Done: $OUTPUT"
ls -lh "$OUTPUT"

#!/usr/bin/env bash
# Autonomous self-intro video recording.
#
# Per Chi's 2026-04-21 directive ("self-intro is without me — figure out
# how to do that"), this script records a ~30s self-introduction video of
# Sutando with ZERO human input during recording. Pre-recorded narration
# plays over screen capture of live Sutando UI activity.
#
# Target machine: whichever has the Sutando web UI + menu-bar app running
# (typically MacBook per project_mini_delegation.md).
#
# Usage:
#   bash scripts/record-self-intro.sh                           # defaults
#   bash scripts/record-self-intro.sh /path/to/audio.mp3        # custom audio
#   bash scripts/record-self-intro.sh /path/to/audio.mp3 30     # custom duration
#
# Outputs a final .mp4 to ~/Documents/sutando-launch-assets/.
# Exit: 0 on success, non-zero on any failure.

set -euo pipefail

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

AUDIO_SRC="${1:-}"
DURATION_SEC="${2:-30}"
OUT_DIR="$HOME/Documents/sutando-launch-assets"
TS="$(date +%Y%m%d-%H%M%S)"
TMP_DIR="$(mktemp -d -t sutando-selfintro-XXXX)"
RAW_MOV="$TMP_DIR/raw.mov"
FINAL_MP4="$OUT_DIR/self-intro-autonomous-$TS.mp4"
FALLBACK_AUDIO="$TMP_DIR/narration.mp3"

# Narration text used if no audio file is supplied — synthesized via
# macOS `say` at 170 wpm. Matches the 32-word target from the v3 storyboard.
NARRATION_TEXT="I'm Sutando. Voice, phone, Discord, Telegram, iMessage, Gmail, browser — every channel drops into the same inbox, same memory, same context. Watch. Chi speaks. I hear. I act. I remember the next time."

# -----------------------------------------------------------------------------
# Preflight
# -----------------------------------------------------------------------------

echo "[record-self-intro] preflight…"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "  FATAL: macOS only (uses screencapture + afplay + avfoundation)"
    exit 1
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "  FATAL: ffmpeg not installed (brew install ffmpeg)"; exit 1; }
command -v afplay  >/dev/null 2>&1 || { echo "  FATAL: afplay missing (should be built-in on macOS)"; exit 1; }

mkdir -p "$OUT_DIR"

# Resolve audio source. Fallback: synthesize narration via `say`.
if [[ -z "$AUDIO_SRC" ]]; then
    # Default: try existing launch-prep variants, then fallback to `say`
    for candidate in \
        /tmp/echo-intro/audio-v1b-tight.mp3 \
        /tmp/echo-intro/audio-v1-samantha.mp3 \
        /tmp/echo-intro/audio.mp3 \
        "$HOME/Documents/sutando-launch-assets/audio-v1b-tight.mp3"; do
        if [[ -f "$candidate" ]]; then
            AUDIO_SRC="$candidate"
            echo "  audio: using existing $candidate"
            break
        fi
    done

    if [[ -z "$AUDIO_SRC" ]]; then
        echo "  audio: no pre-recorded file found, synthesizing via \`say\`"
        # Samantha voice at 170 wpm approximates the v3 storyboard pace.
        # `say` outputs AIFF; convert to MP3 so ffmpeg mux is trivial.
        TMP_AIFF="$TMP_DIR/narration.aiff"
        say -v Samantha -r 170 -o "$TMP_AIFF" "$NARRATION_TEXT"
        ffmpeg -y -loglevel error -i "$TMP_AIFF" -codec:a libmp3lame -qscale:a 2 "$FALLBACK_AUDIO"
        AUDIO_SRC="$FALLBACK_AUDIO"
    fi
fi

if [[ ! -f "$AUDIO_SRC" ]]; then
    echo "  FATAL: audio source $AUDIO_SRC not found"
    exit 1
fi

echo "  audio src: $AUDIO_SRC"
echo "  duration:  ${DURATION_SEC}s"
echo "  raw.mov:   $RAW_MOV"
echo "  final:     $FINAL_MP4"

# -----------------------------------------------------------------------------
# Record (screen + audio in parallel)
# -----------------------------------------------------------------------------

# `screencapture -V <sec>` records video for N seconds (macOS 15+). `-x`
# silences the shutter sound. `-T 0` starts immediately. Main display only.
# For multi-display, swap in `ffmpeg -f avfoundation -i "1:none"`.
echo "[record-self-intro] starting screen-record + narration in parallel…"

# Play narration (backgrounded, auto-stops when file ends)
afplay "$AUDIO_SRC" &
AFPLAY_PID=$!

# Record screen for DURATION_SEC — blocks until done.
# -x = silent shutter; -V = video duration (macOS 15+ only).
# If `-V` is rejected (older macOS), fall back to ffmpeg avfoundation.
if ! screencapture -x -V "$DURATION_SEC" -T 0 "$RAW_MOV" 2>"$TMP_DIR/screencapture.err"; then
    echo "  screencapture -V rejected; falling back to ffmpeg avfoundation"
    # Device 1 is typically the main display on Mac; `none` = no audio
    # (we mux the narration mp3 in the next step).
    ffmpeg -y -loglevel error -f avfoundation -framerate 30 -i "1:none" \
        -t "$DURATION_SEC" -c:v libx264 -preset ultrafast -crf 22 "$RAW_MOV"
fi

# Make sure narration has finished (or reaches EOF)
wait $AFPLAY_PID 2>/dev/null || true

if [[ ! -f "$RAW_MOV" || ! -s "$RAW_MOV" ]]; then
    echo "  FATAL: screen-record produced no output"
    cat "$TMP_DIR/screencapture.err" 2>/dev/null || true
    exit 1
fi

echo "  raw size: $(du -h "$RAW_MOV" | cut -f1)"

# -----------------------------------------------------------------------------
# Mux (video + narration)
# -----------------------------------------------------------------------------

echo "[record-self-intro] muxing…"

# -shortest: end when either stream ends (narration may be shorter)
# -c:v libx264 + crf 22 preset slow: decent quality, web-ready
# -c:a aac: web-friendly audio codec
ffmpeg -y -loglevel error \
    -i "$RAW_MOV" \
    -i "$AUDIO_SRC" \
    -c:v libx264 -crf 22 -preset slow \
    -c:a aac -b:a 128k \
    -shortest \
    "$FINAL_MP4"

if [[ ! -f "$FINAL_MP4" ]]; then
    echo "  FATAL: mux produced no output"
    exit 1
fi

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------

FINAL_SIZE=$(du -h "$FINAL_MP4" | cut -f1)
FINAL_DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$FINAL_MP4" 2>/dev/null || echo "unknown")

echo
echo "✅ DONE"
echo "   path:     $FINAL_MP4"
echo "   size:     $FINAL_SIZE"
echo "   duration: ${FINAL_DURATION}s"
echo
echo "Review + ship when ready:"
echo "   open \"$FINAL_MP4\""
echo
echo "Cleanup temp: rm -rf $TMP_DIR"

# Print the path last on its own line so callers (e.g., Discord bridge) can
# grab it via \`tail -n 1\` if needed.
echo "$FINAL_MP4"

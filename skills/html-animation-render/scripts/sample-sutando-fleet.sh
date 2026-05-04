#!/bin/bash
# Render the Sutando fleet-growth animation. Convenience wrapper around render.mjs.
# Run from anywhere — paths are absolute.
set -euo pipefail

cd "$HOME/Desktop/sutando"
node skills/html-animation-render/scripts/render.mjs \
  --html "$HOME/.sutando-memory-sync/notes/sutando-fleet-growth.html" \
  --out  "$HOME/.sutando-memory-sync/notes/media/sutando-fleet-growth.mp4" \
  --audio "$HOME/.sutando-memory-sync/notes/media/happy-light-30s.mp3" \
  --duration 33

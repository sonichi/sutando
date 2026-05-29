#!/bin/bash
# Pointer Teacher tracer — DRIVER. Ties the slice together:
#   intent -> resolver (capture+Gemini) -> IPC file -> overlay flies -> narration
#
#   ./run.sh "where do I commit here?"
#
# Builds + starts the overlay on first use. `say` stands in for the voice
# agent's TTS in the tracer (production = bodhi VoiceSession narration).
set -euo pipefail
cd "$(dirname "$0")"
Q="${*:-where do I commit here?}"

# 1. build overlay if needed
if [ ! -x ./pointer-overlay ] || [ pointer-overlay.swift -nt ./pointer-overlay ]; then
  echo "· building overlay…"
  swiftc pointer-overlay.swift -o pointer-overlay
fi

# 2. ensure overlay running
if ! pgrep -f "pointer-teacher-tracer/pointer-overlay" >/dev/null 2>&1; then
  echo "· starting overlay…"
  ./pointer-overlay >/tmp/pointer-overlay.log 2>&1 &
  sleep 1
fi

# 3. resolve Target -> writes /tmp/pointer-cmd.json (overlay polls + flies)
echo "· resolving: $Q"
OUT="$(python3 resolver.py "$Q")"
echo "$OUT" | sed -n '1p'

# 4. narrate (overlaps the flight, Clicky-style)
SAY="$(echo "$OUT" | tail -1 | python3 -c 'import json,sys;print(json.load(sys.stdin).get("say",""))' 2>/dev/null || true)"
[ -n "$SAY" ] && { echo "· say: $SAY"; say "$SAY" & }
echo "· done — watch the pointer fly."

#!/bin/bash
# Render text to speech via Google Gemini Flash TTS. Reads GEMINI_API_KEY from .env.
# Usage: synthesize.sh [--voice <name>] [--out <path>] [--model <id>] -- "text"
#
# Free-tier eligible: gemini-2.5-flash-tts within 1500 req/day. Parallels
# skills/openai-tts/scripts/synthesize.sh (same flag shape, different backend).
set -euo pipefail

VOICE="Aoede"
OUT=""
MODEL="${GEMINI_TTS_MODEL:-gemini-2.5-flash-preview-tts}"
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice) VOICE="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --) shift; ARGS+=("$@"); break ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

TEXT="${ARGS[*]-}"
[[ -n "$TEXT" ]] || { echo "Usage: synthesize.sh [--voice <name>] [--out <path>] [--model <id>] -- \"text\"" >&2; exit 2; }

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
KEY="${GEMINI_API_KEY:-$(grep -E '^GEMINI_API_KEY=' "$REPO/.env" 2>/dev/null | cut -d= -f2-)}"
[[ -n "$KEY" ]] || { echo "GEMINI_API_KEY missing (set env or add to .env)" >&2; exit 1; }

[[ -n "$OUT" ]] || OUT="$REPO/results/gemini-tts-$(date +%s).mp3"
mkdir -p "$(dirname "$OUT")"

# Gemini TTS API: generateContent with audio modality + voice config
# Endpoint: https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent
PAYLOAD=$(python3 -c '
import json, sys
text, voice = sys.argv[1], sys.argv[2]
print(json.dumps({
  "contents": [{"parts": [{"text": text}]}],
  "generationConfig": {
    "responseModalities": ["AUDIO"],
    "speechConfig": {
      "voiceConfig": {
        "prebuiltVoiceConfig": {"voiceName": voice}
      }
    }
  }
}))
' "$TEXT" "$VOICE")

URL="https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${KEY}"

RESPONSE=$(curl -sSf "$URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" 2>&1) || {
    echo "Gemini TTS request failed:" >&2
    echo "$RESPONSE" >&2
    exit 1
  }

# Extract base64 audio data from response.candidates[0].content.parts[0].inlineData.data
B64=$(echo "$RESPONSE" | python3 -c '
import json, sys
try:
  d = json.load(sys.stdin)
  parts = d["candidates"][0]["content"]["parts"]
  for p in parts:
    inline = p.get("inlineData") or p.get("inline_data")
    if inline and "data" in inline:
      print(inline["data"])
      sys.exit(0)
  print("ERR: no inlineData in response", file=sys.stderr)
  sys.exit(2)
except (KeyError, IndexError, json.JSONDecodeError) as e:
  print(f"ERR: parse failed: {e}", file=sys.stderr)
  sys.exit(2)
')

[[ -n "$B64" ]] || { echo "Empty audio response from Gemini" >&2; exit 1; }

# Decode base64 → raw audio. Gemini returns 24kHz PCM by default; we wrap as MP3
# via ffmpeg pipe. If ffmpeg missing, fall back to writing raw PCM with .pcm
# suffix so the user knows.
if command -v ffmpeg >/dev/null 2>&1; then
  echo "$B64" | base64 -d | ffmpeg -hide_banner -loglevel error -y \
    -f s16le -ar 24000 -ac 1 -i - "$OUT"
else
  # Fall back: write raw PCM
  PCM_OUT="${OUT%.mp3}.pcm"
  echo "$B64" | base64 -d > "$PCM_OUT"
  echo "WARN: ffmpeg missing; wrote raw 24kHz PCM to $PCM_OUT instead of mp3" >&2
  OUT="$PCM_OUT"
fi

echo "$OUT"

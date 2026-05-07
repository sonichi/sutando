#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: synthesize.sh [options] -- [text]
       synthesize.sh [options] --file path/to/text.txt

Render text to speech via OpenAI's tts-1-hd model.

Options:
  --voice <name>    alloy | ash | coral | echo | fable | nova | onyx | sage | shimmer
                    (default: coral)
  --out <path>      Output mp3 path (default: results/openai-tts-{epoch}.mp3)
  --file <path>     Read input text from a file instead of the argv tail
  --model <name>    tts-1-hd | tts-1 (default: tts-1-hd)
  --help            Show this help

Reads OPENAI_API_KEY from .env at the repo root.

Examples:
  synthesize.sh -- "Hello, this is Sutando."
  synthesize.sh --voice ash --out /tmp/intro.mp3 -- "Hi."
  synthesize.sh --voice coral --out /tmp/scene.mp3 --file script.txt
EOF
}

fail() { echo "synthesize.sh: $*" >&2; exit 1; }

VOICE="coral"
OUT=""
FILE=""
MODEL="tts-1-hd"
TEXT_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice) VOICE="$2"; shift 2 ;;
    --out)   OUT="$2";   shift 2 ;;
    --file)  FILE="$2";  shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    --) shift; TEXT_ARGS+=("$@"); break ;;
    *) TEXT_ARGS+=("$1"); shift ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

if [[ -n "$FILE" ]]; then
  [[ -f "$FILE" ]] || fail "input file not found: $FILE"
  TEXT="$(cat "$FILE")"
else
  TEXT="${TEXT_ARGS[*]-}"
fi

[[ -n "$TEXT" ]] || { usage; exit 2; }

API_KEY="${OPENAI_API_KEY:-}"
if [[ -z "$API_KEY" && -f "$REPO_ROOT/.env" ]]; then
  API_KEY="$(grep -E '^OPENAI_API_KEY=' "$REPO_ROOT/.env" | head -1 | cut -d= -f2-)"
fi
[[ -n "$API_KEY" ]] || fail "OPENAI_API_KEY not found in env or .env"

if [[ -z "$OUT" ]]; then
  mkdir -p "$REPO_ROOT/results"
  OUT="$REPO_ROOT/results/openai-tts-$(date +%s).mp3"
fi
mkdir -p "$(dirname "$OUT")"

PAYLOAD="$(python3 -c '
import json, sys
print(json.dumps({"model": sys.argv[1], "voice": sys.argv[2], "input": sys.argv[3]}))
' "$MODEL" "$VOICE" "$TEXT")"

HTTP_CODE="$(curl -s -w '%{http_code}' -o "$OUT" \
  https://api.openai.com/v1/audio/speech \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")"

if [[ "$HTTP_CODE" != "200" ]]; then
  ERR_BODY="$(cat "$OUT" 2>/dev/null || true)"
  rm -f "$OUT"
  fail "OpenAI returned HTTP $HTTP_CODE: $ERR_BODY"
fi

if [[ ! -s "$OUT" ]]; then
  fail "OpenAI returned an empty body"
fi

echo "$OUT"

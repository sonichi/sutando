#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
STARTUP="$REPO/src/startup.sh"

grep -q 'continuing with credential-free services' "$STARTUP"
grep -q 'export SKIP_VOICE=1' "$STARTUP"
grep -q 'if \[ "${SKIP_VOICE:-}" = "1" \]' "$STARTUP"
grep -q 'WEB_CLIENT_PORT="${CLIENT_PORT:-8080}"' "$STARTUP"
grep -q 'VERIFY_PORTS="$WEB_CLIENT_PORT:web-client 7844:dashboard 7843:agent-api 7845:screen-capture"' "$STARTUP"
if grep -q 'GEMINI_API_KEY not set in .env' "$STARTUP" || grep -q 'missing=1.*GEMINI_API_KEY' "$STARTUP"; then
  echo "startup still treats Gemini credentials as globally required" >&2
  exit 1
fi
grep -q 'command -v "$core_runtime"' "$STARTUP"

VERIFY="$REPO/src/verify-setup.sh"
grep -q 'core-runtime' "$VERIFY"
grep -q 'command -v "$CORE_RUNTIME"' "$VERIFY"
grep -q 'codex login status' "$VERIFY"
if grep -q 'fail "GEMINI_API_KEY not set' "$VERIFY"; then
  echo "verify-setup still treats Gemini credentials as globally required" >&2
  exit 1
fi

echo "PASS: Gemini credentials gate only the optional voice service"

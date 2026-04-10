#!/bin/bash
# Sutando Gemini 3.1 rollout verification
#
# Runs after merging the full 3.1 compat stack:
#   - bodhi fork #2 (sendAudio media→audio) — Susan
#   - bodhi fork #3 (sendFile mimeType branching) — Chi
#   - sutando #259 (duplicate tool declaration dedup + SDK bump) — Chi
#
# Then runs:
#   1. npm install github:liususan091219/bodhi_realtime_agent  (pulls new bodhi SHA)
#   2. Verifies the installed bodhi dist contains all 3 fixes
#   3. Verifies sutando's tools deduplicate correctly
#   4. Verifies .env is still pinned to 2.5 (so this run is safe to run even
#      before the user is ready to flip 3.1 on)
#   5. Prints the manual next-steps checklist
#
# Usage: bash src/verify-gemini-31.sh [--install]
#   --install  run `npm install github:liususan091219/bodhi_realtime_agent` first
#              (only needed once after bodhi fork main advances)

set -e

PASS=0
FAIL=0
WARN=0

pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ~ $1"; WARN=$((WARN+1)); }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "Sutando Gemini 3.1 Rollout Verification"
echo "========================================"

if [ "${1:-}" = "--install" ]; then
  echo ""
  echo "Pulling latest bodhi fork..."
  npm install github:liususan091219/bodhi_realtime_agent 2>&1 | tail -3
fi

# 1. Bodhi dist — sendClientContent text path (PR #1)
echo ""
echo "Bodhi fork PR #1 (sendClientContent → sendRealtimeInput text path):"
if grep -q 'sendClientContent(turns, _turnComplete' node_modules/bodhi-realtime-agent/dist/index.js; then
  # The narrowed method signature with _turnComplete (underscore-prefixed unused
  # parameter) is a reliable marker for PR #1.
  pass "sendClientContent narrowed to text-only routing to sendRealtimeInput"
else
  fail "sendClientContent still uses the old shape — PR #1 not applied in this bodhi build"
fi

# 2. Bodhi dist — sendAudio audio key (PR #2)
# Grep for the Gemini transport's sendAudio specifically. The OpenAI realtime
# transport also has a sendAudio which uses `audio: base64Data` as a flat
# wire field — that would false-positive a naive match.
echo ""
echo "Bodhi fork PR #2 (sendAudio media→audio):"
GEMINI_SENDAUDIO=$(awk '
  /sendAudio\(base64Data\) {/ { capture=1; depth=0 }
  capture { print; if (/{/) depth++; if (/}/) { depth--; if (depth==0) exit } }
' node_modules/bodhi-realtime-agent/dist/index.js | grep -A10 'this.session.sendRealtimeInput' || true)
if echo "$GEMINI_SENDAUDIO" | grep -q 'audio: { data'; then
  pass "sendAudio uses the audio key"
elif echo "$GEMINI_SENDAUDIO" | grep -q 'media: { data'; then
  fail "sendAudio still uses the deprecated media key — bodhi #2 not merged/installed"
else
  warn "could not positively identify sendAudio wire format"
fi

# 3. Bodhi dist — sendFile branching (PR #3)
# Same story — grep specifically for the Gemini transport's sendFile.
echo ""
echo "Bodhi fork PR #3 (sendFile mimeType branching):"
GEMINI_SENDFILE=$(awk '
  /sendFile\(base64Data, mimeType\) {/ { capture=1; depth=0 }
  capture { print; if (/{/) depth++; if (/}/) { depth--; if (depth==0) exit } }
' node_modules/bodhi-realtime-agent/dist/index.js)
if echo "$GEMINI_SENDFILE" | grep -q 'mimeType.startsWith("image/")'; then
  pass "sendFile branches on image/* → video key"
elif echo "$GEMINI_SENDFILE" | grep -q 'media: { data'; then
  fail "sendFile still uses deprecated single media key — bodhi #3 not merged/installed"
else
  warn "could not positively identify sendFile routing"
fi
if echo "$GEMINI_SENDFILE" | grep -q 'mimeType.startsWith("audio/")'; then
  pass "sendFile branches on audio/* → audio key"
fi

# 4. Sutando duplicate tool declaration dedup (PR #259)
echo ""
echo "Sutando PR #259 (duplicate tool dedup):"
if grep -q 'Deduplicate by name' skills/phone-conversation/scripts/conversation-server.ts 2>/dev/null; then
  pass "conversation-server deduplicates inlineTools against anyCallerTools"
else
  fail "phone conversation-server does not dedupe — sutando #259 not merged"
fi

# 5. Sanity: grep for the problematic duplicate (getCurrentTimeTool in both arrays)
if grep -q 'getCurrentTimeTool' src/inline-tools.ts; then
  ANY_COUNT=$(grep -c 'getCurrentTimeTool' src/inline-tools.ts)
  if [ "$ANY_COUNT" -ge 2 ]; then
    warn "getCurrentTimeTool still appears in multiple arrays in inline-tools.ts — PR #259 relies on the consumer-side dedup to handle this; fine for phone but voice-agent tool list must not include anyCallerTools (which it doesn't, by construction)"
  fi
fi

# 6. .env model + googleSearch pin state
echo ""
echo ".env model + googleSearch config:"
VOICE_MODEL=$(grep '^VOICE_NATIVE_AUDIO_MODEL=' .env 2>/dev/null | cut -d= -f2)
VOICE_GSEARCH=$(grep '^VOICE_GOOGLE_SEARCH=' .env 2>/dev/null | cut -d= -f2)
ON_31=0
if [ -z "$VOICE_MODEL" ]; then
  warn "VOICE_NATIVE_AUDIO_MODEL not set in .env — bodhi will use its default"
elif echo "$VOICE_MODEL" | grep -q '2\.5-flash-native-audio'; then
  pass "VOICE_NATIVE_AUDIO_MODEL=$VOICE_MODEL (safe 2.5 baseline)"
elif echo "$VOICE_MODEL" | grep -q '3\.1-flash-live'; then
  pass "VOICE_NATIVE_AUDIO_MODEL=$VOICE_MODEL (3.1 enabled)"
  ON_31=1
else
  warn "VOICE_NATIVE_AUDIO_MODEL=$VOICE_MODEL (unrecognized)"
fi
# VOICE_GOOGLE_SEARCH must be false when 3.1 is active — 3.1 native audio
# rejects the googleSearch grounding tool entry with a misleading 1011.
# See sutando #262 for the investigation and env-var gate introduction.
if [ "$ON_31" = "1" ]; then
  if [ -z "$VOICE_GSEARCH" ] || echo "$VOICE_GSEARCH" | grep -qi 'true'; then
    fail "VOICE_GOOGLE_SEARCH must be 'false' when running gemini-3.1-flash-live-preview (current: '${VOICE_GSEARCH:-unset, defaults to true}') — voice-agent will hit 1011 \"exceeded your current quota\" on connect"
  elif echo "$VOICE_GSEARCH" | grep -qi 'false'; then
    pass "VOICE_GOOGLE_SEARCH=false (required for 3.1)"
  else
    warn "VOICE_GOOGLE_SEARCH=$VOICE_GSEARCH (unrecognized — must be 'true' or 'false')"
  fi
else
  # On 2.5, VOICE_GOOGLE_SEARCH default true is fine; only warn if explicitly set to false (lost capability for no reason)
  if echo "$VOICE_GSEARCH" | grep -qi 'false'; then
    warn "VOICE_GOOGLE_SEARCH=false but you're on 2.5 — you don't need this and you're losing Google Search grounding unnecessarily"
  fi
fi

# 7. voice-transport health probe
echo ""
echo "voice-agent transport state:"
if python3 src/health-check.py --quiet 2>&1 | grep -q "voice-transport .*ok"; then
  pass "voice-transport probe: no recent abnormal closes"
elif lsof -iTCP:9900 -sTCP:LISTEN >/dev/null 2>&1; then
  warn "voice-transport probe not green — check logs/voice-agent.log for recent 1007/1011/1006 close codes"
else
  warn "voice-agent not running — start it before testing 3.1"
fi

# Summary
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed, $WARN warnings"
echo ""

if [ "$FAIL" -gt 0 ]; then
  echo "NOT READY for 3.1 rollout. Resolve the failures above before unpinning .env."
  echo ""
  echo "Most common fix: run 'npm install github:liususan091219/bodhi_realtime_agent' to"
  echo "pull the latest bodhi fork after PRs #2 and #3 merge there."
  exit 1
fi

echo "Ready for 3.1 rollout. Manual next steps:"
echo ""
echo "  1. Edit .env — set BOTH env vars together (3.1 rejects googleSearch):"
echo "       VOICE_NATIVE_AUDIO_MODEL=gemini-3.1-flash-live-preview"
echo "       VOICE_GOOGLE_SEARCH=false"
echo "  2. Restart voice-agent:"
echo "     launchctl kickstart -k gui/\$(id -u)/com.sutando.voice-agent"
echo "  3. Open http://localhost:8080, connect voice"
echo "  4. Test audio-in: say 'hello'"
echo "  5. Test tool call: say 'what time is it'"
echo "  6. Test goodbye close: say 'bye' → should hear Gemini say 'Goodbye' and session closes cleanly"
echo "  7. Reconnect and verify no replay contamination"
echo ""
echo "If anything misbehaves, roll back:"
echo "  cp /tmp/sutando-env-backup-*.env .env  (or edit .env manually)"
echo "  launchctl kickstart -k gui/\$(id -u)/com.sutando.voice-agent"
exit 0

#!/bin/bash
# Verify Twilio webhook URL matches the current ngrok tunnel.
# Stale webhooks are a common failure mode — inbound calls go nowhere.
#
# Usage:
#   bash src/check-webhook.sh          # check and report
#   bash src/check-webhook.sh --fix    # check and update if stale
#
# Exit codes: 0=match, 1=mismatch, 2=error

set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/.env" 2>/dev/null

# Check prerequisites
if [ -z "${TWILIO_ACCOUNT_SID:-}" ] || [ -z "${TWILIO_AUTH_TOKEN:-}" ] || [ -z "${TWILIO_PHONE_NUMBER:-}" ]; then
  echo "skip: Twilio not configured"
  exit 0
fi

# Get current ngrok tunnel URL
NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    t = [x for x in d.get('tunnels', []) if x.get('proto') == 'https']
    print(t[0]['public_url'] if t else '')
except: pass
" 2>/dev/null)

if [ -z "$NGROK_URL" ]; then
  echo "warn: ngrok not running — inbound calls disabled"
  exit 2
fi

# Get Twilio phone number SID
ENCODED_NUM=$(TWILIO_PHONE_NUMBER="$TWILIO_PHONE_NUMBER" python3 -c '
import os, urllib.parse
print(urllib.parse.quote(os.environ["TWILIO_PHONE_NUMBER"]))
')
PHONE_SID=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers.json?PhoneNumber=$ENCODED_NUM" \
  2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    nums = d.get('incoming_phone_numbers', [])
    print(nums[0]['sid'] if nums else '')
except: pass
" 2>/dev/null)

if [ -z "$PHONE_SID" ]; then
  echo "error: could not find Twilio phone number SID"
  exit 2
fi

# Get current Twilio voice URL
VOICE_URL=$(curl -s -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
  "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$PHONE_SID.json" \
  2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('voice_url', ''))
except: pass
" 2>/dev/null)

# Compare
if echo "$VOICE_URL" | grep -q "$NGROK_URL"; then
  echo "ok: webhook matches tunnel ($NGROK_URL)"
  exit 0
else
  echo "MISMATCH: Twilio=$VOICE_URL ngrok=$NGROK_URL"

  if [ "${1:-}" = "--fix" ]; then
    NEW_URL="$NGROK_URL/twilio/connect?purpose=inbound"
    echo "Updating Twilio webhook to: $NEW_URL"
    curl -s -X POST -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN" \
      "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/IncomingPhoneNumbers/$PHONE_SID.json" \
      -d "VoiceUrl=$NEW_URL" > /dev/null 2>&1
    echo "Updated."
    exit 0
  else
    echo "Run with --fix to update Twilio webhook"
    exit 1
  fi
fi

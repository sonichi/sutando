#!/usr/bin/env bash
# Drives the Twilio diagnostic extracted from src/startup.sh by its own anchors,
# so deleting the block fails this file instead of leaving it green.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/src/startup.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "startup twilio webhook drift:"

# --- extract the live block, source-tied -------------------------------------
BLOCK="$(awk '/^ *TWILIO_CFG_URL=/,/^ *fi$/' "$SRC")"
if [ -z "$BLOCK" ]; then
  bad "block extracted from src/startup.sh" "no TWILIO_CFG_URL..fi region found"
  echo "  Total: 1 — pass: 0, fail: 1"; exit 1
fi
ok "block extracted from src/startup.sh"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
run_case() {  # $1=.env body  $2=NGROK_URL
  ( cd "$TMP" && printf '%s\n' "$1" > .env && NGROK_URL="$2" bash -c "$BLOCK" 2>&1 )
}

LIVE="https://tunnel-new.ngrok-free.app"
OLD="https://tunnel-old.ngrok-free.app"

# 1. equal, with the trailing-comment shape this host actually has -> SILENT
out="$(run_case "TWILIO_WEBHOOK_URL=$LIVE  # run: ngrok http 3100" "$LIVE")"
[ -z "$out" ] && ok "equal (comment-suffixed) is silent" \
  || bad "equal (comment-suffixed) is silent" "got: $out"

# 2. equal, bare value -> SILENT
out="$(run_case "TWILIO_WEBHOOK_URL=$LIVE" "$LIVE")"
[ -z "$out" ] && ok "equal (bare) is silent" || bad "equal (bare) is silent" "got: $out"

# 3. absent -> warns, and asks for the console to be pointed at the live URL
out="$(run_case "OTHER=1" "$LIVE")"
case "$out" in
  *"$LIVE"*) ok "absent warns and names the live URL" ;;
  *) bad "absent warns and names the live URL" "got: $out" ;;
esac

# 4. mismatched -> names BOTH old and new
out="$(run_case "TWILIO_WEBHOOK_URL=$OLD" "$LIVE")"
case "$out" in
  *"$OLD"*"$LIVE"*) ok "mismatch names was/now" ;;
  *) bad "mismatch names was/now" "got: $out" ;;
esac

# 5. the runtime consequence: the mismatch repair must name the LOCAL side too.
#    Console-only advice is the defect this case exists to forbid.
case "$out" in
  *TWILIO_WEBHOOK_URL*) ok "mismatch names the env var the server binds" ;;
  *) bad "mismatch names the env var the server binds" "console-only repair" ;;
esac
case "$out" in
  *restart*) ok "mismatch names the required restart" ;;
  *) bad "mismatch names the required restart" "no restart named" ;;
esac

# 6. equivalent URLs are NOT drift: conversation-server strips the trailing slash
#    before binding, so comparing unnormalised prescribes a restart for nothing.
out="$(run_case "TWILIO_WEBHOOK_URL=$LIVE/" "$LIVE")"
[ -z "$out" ] && ok "trailing-slash-equivalent is silent" \
  || bad "trailing-slash-equivalent is silent" "got: $out"

# 7. a non-ngrok tunnel is authoritative: the server binds it and never starts
#    ngrok, so startup's disposable ngrok is not its tunnel and not drift.
out="$(run_case "TWILIO_WEBHOOK_URL=https://host.tail1234.ts.net" "$LIVE")"
[ -z "$out" ] && ok "configured Funnel tunnel is not reported as drift" \
  || bad "configured Funnel tunnel is not reported as drift" "got: $out"

# 8. pin that conversation-server really binds this var and skips ngrok, so the
#    two-sided advice fails here if that stops being true.
CS="$REPO/skills/phone-conversation/scripts/conversation-server.ts"
if [ -f "$CS" ]; then
  if grep -q 'process.env.TWILIO_WEBHOOK_URL' "$CS" && grep -q 'WEBHOOK_BASE_URL = externalUrl' "$CS"; then
    ok "conversation-server binds TWILIO_WEBHOOK_URL to WEBHOOK_BASE_URL"
  else
    bad "conversation-server binds TWILIO_WEBHOOK_URL to WEBHOOK_BASE_URL" "binding not found"
  fi
else
  ok "conversation-server absent (optional skill) — binding check skipped"
fi

total=$((10))
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1

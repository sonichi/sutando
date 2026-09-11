#!/usr/bin/env bash
# Drives the Funnel block extracted from src/startup.sh by its own anchors, so
# deleting the block fails this file instead of leaving it green.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/src/startup.sh"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "startup funnel drift:"

# The live region is an if/elif chain; take it to the elif, drop that line, and
# close the if so the extract runs standalone. Anchored on the live source, so
# deleting the block empties this and fails below rather than passing vacuously.
BLOCK="$(awk '/^ *FUNNEL_CFG_URL=/,/^ *elif ! pgrep -f "ngrok"/' "$SRC" | sed '$d')
fi"
if [ -z "$BLOCK" ]; then
  bad "block extracted from src/startup.sh" "no FUNNEL_CFG_URL region found"
  echo "  Total: 1 — pass: 0, fail: 1"; exit 1
fi
ok "block extracted from src/startup.sh"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# A stub tailscale: prints whatever funnel status we want it to.
mk_ts() {  # $1 = the https://... line to emit, or empty for "nothing serving"
  printf '#!/usr/bin/env bash\n[ "$1" = funnel ] || exit 0\n' > "$TMP/ts"
  [ -n "$1" ] && printf 'echo "%s (Funnel on)"\n' "$1" >> "$TMP/ts"
  chmod +x "$TMP/ts"; echo "$TMP/ts"
}
run_case() {  # $1=.env body  $2=TAILSCALE_BIN
  ( cd "$TMP" && printf '%s\n' "$1" > .env && TAILSCALE_BIN="$2" bash -c "$BLOCK" 2>&1 )
}

PRO="https://chis-macbook-pro.taild96b9b.ts.net"
AIR="https://chis-macbook-air.taild96b9b.ts.net"

out="$(run_case "TWILIO_WEBHOOK_URL=$PRO" "$(mk_ts "$PRO")")"
case "$out" in
  *"matches TWILIO_WEBHOOK_URL"*) ok "configured == live funnel -> confirms, no warning" ;;
  *) bad "configured == live funnel -> confirms" "got: $out" ;;
esac
case "$out" in *"DIFFERENT host"*) bad "match must not warn" "got: $out" ;; *) ok "match does not warn" ;; esac

out="$(run_case "TWILIO_WEBHOOK_URL=$AIR" "$(mk_ts "$PRO")")"
case "$out" in
  *"DIFFERENT host"*) ok "configured != live funnel -> warns" ;;
  *) bad "configured != live funnel -> warns" "got: $out" ;;
esac
case "$out" in *"$AIR"*) ok "warning names the configured host" ;; *) bad "warning names configured host" ;; esac
case "$out" in *"$PRO"*) ok "warning names this host's funnel" ;; *) bad "warning names this host" ;; esac

# The control that matters: a missing CLI must read as UNCHECKED, never as a tick.
out="$(run_case "TWILIO_WEBHOOK_URL=$AIR" "$TMP/definitely-not-here")"
case "$out" in
  *UNCHECKED*) ok "absent tailscale -> UNCHECKED, not agreement" ;;
  *) bad "absent tailscale -> UNCHECKED" "got: $out" ;;
esac
case "$out" in *"✓"*) bad "absent tailscale must not tick" "got: $out" ;; *) ok "absent tailscale does not tick" ;; esac

out="$(run_case "TWILIO_WEBHOOK_URL=$AIR" "$(mk_ts "")")"
case "$out" in
  *UNCHECKED*) ok "no funnel serving -> UNCHECKED, not agreement" ;;
  *) bad "no funnel serving -> UNCHECKED" "got: $out" ;;
esac

# A non-Funnel URL must leave FUNNEL_MODE off so the ngrok path still runs.
out="$(run_case "TWILIO_WEBHOOK_URL=https://tunnel.ngrok-free.app" "$(mk_ts "$PRO")")"
case "$out" in
  ""|*"Starting ngrok"*) ok "ngrok URL -> funnel block silent, ngrok path untouched" ;;
  *) bad "ngrok URL -> funnel block silent" "got: $out" ;;
esac

# Trailing slash and a trailing comment are the shapes .env actually carries.
out="$(run_case "TWILIO_WEBHOOK_URL=$PRO/  # tailscale funnel" "$(mk_ts "$PRO")")"
case "$out" in
  *"matches TWILIO_WEBHOOK_URL"*) ok "trailing slash + comment normalise" ;;
  *) bad "trailing slash + comment normalise" "got: $out" ;;
esac

total=$((9))
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ]

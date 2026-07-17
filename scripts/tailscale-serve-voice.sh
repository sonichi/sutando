#!/usr/bin/env bash
#
# Front the Sutando webUI with Tailscale-terminated HTTPS, so a browser on
# another device on your tailnet can open a voice call straight to this core.
#
# Why this exists: a browser on an HTTPS page blocks insecure ws:// to a
# non-localhost host (mixed content), so the tailnet voice path only works over
# wss://. `tailscale serve` terminates TLS with an auto-provisioned, auto-renewed
# cert on this machine's MagicDNS name and reverse-proxies to loopback — where
# the webUI's existing /ws proxy accepts the (now loopback-sourced) upgrade. No
# cert management, no extra serving code.
#
# Prerequisites:
#   - `tailscale up` (this node on the tailnet)
#   - HTTPS enabled for the tailnet (admin console → DNS → "Enable HTTPS")
#   - the webUI running with SUTANDO_LAN_SHARE=1 on CLIENT_PORT (so /ws is live)
#
# After running this, also export SUTANDO_TAILNET_SERVE=1 for the core so the
# advertised tailnet endpoint becomes https:// (browser-ready) instead of
# http://host:CLIENT_PORT (native-only). Then restart the core.
#
# Undo:  tailscale serve reset
set -euo pipefail

PORT="${CLIENT_PORT:-8080}"

if ! command -v tailscale >/dev/null 2>&1; then
	echo "error: tailscale CLI not found. Install Tailscale and run 'tailscale up' first." >&2
	exit 1
fi

# Fail early with a clear message if the node isn't up / has no tailnet name.
DNSNAME="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; s=json.load(sys.stdin).get("Self",{}); print((s.get("DNSName") or "").rstrip("."))' 2>/dev/null || true)"
if [ -z "$DNSNAME" ]; then
	echo "error: this node has no tailnet DNS name (is 'tailscale up' done?)." >&2
	exit 1
fi

echo "Serving the webUI (127.0.0.1:${PORT}) over HTTPS on your tailnet…"
# --bg = run in the background (persists). Exposes https://<magicdns>/ on :443,
# forwarding to the local webUI. Idempotent-ish: re-running updates the mapping.
tailscale serve --bg "${PORT}"

echo
echo "  ✓ webUI now reachable at:  https://${DNSNAME}/"
echo "    voice WS for clients:     wss://${DNSNAME}/ws"
echo
echo "  Next, so the core advertises the https endpoint:"
echo "    export SUTANDO_TAILNET_SERVE=1   # (and SUTANDO_LAN_SHARE=1) then restart the core"
echo
echo "  To undo:  tailscale serve reset"

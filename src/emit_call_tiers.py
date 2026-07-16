#!/usr/bin/env python3
"""emit_call_tiers — python port of src/emit-call-tiers.ts for node-less installs.

The TS emitter is invoked by startup.sh via `npx tsx`, which DESKTOP-LAUNCHED
cores never run: launch-sutando.sh (ag2space-cinny-desktop) wraps core+gateway
only, and the bundled engine ships python+tmux with NO node toolchain (R1's
bundled node ships pre-built service dists, not a tsx runner). Result observed
live 2026-07-16: the runtime descriptor folds `call_tiers: []` on a desktop
install, so the availability-driven Start-Call menu (Track 9) never sees the
core's direct routes. This port lets the desktop launcher emit the SAME
`state/call-tiers.json` with the python the engine already bundles.

Behavior parity with emit-call-tiers.ts + reachability-endpoints.ts:
  - only DIRECT tiers advertised (local is client-relative; cloud/relay are
    client-composed) — the advertisement is a HINT, the client still verifies;
  - both tiers gated by SUTANDO_LAN_SHARE;
  - tailnet: `tailscale status --json` (1.5s timeout), Online gate, MagicDNS
    name preferred over the 100.x IPv4; SUTANDO_TAILNET_SERVE flips the URL to
    https://<host> (tailscale serve fronts :443), else http://<host>:CLIENT_PORT;
  - lan: lowest-named interface's RFC1918 IPv4 (tailnet 100.64/10, loopback and
    link-local excluded), http://<ip>:CLIENT_PORT;
  - payload {ts, pid, call_tiers} written to status_path('call-tiers.json').

Keep the two emitters in sync: a tier/label/shape change here must land in
emit-call-tiers.ts too (and vice versa) — `sutando-config.sh runtime` and the
desktop `console_status` passthrough read whichever ran last.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from workspace_default import status_path  # noqa: E402


def _client_port() -> int:
    try:
        return int(os.environ.get("CLIENT_PORT", "") or 8080)
    except ValueError:
        return 8080


def _flag(name: str) -> bool:
    return bool(re.match(r"^(1|true|yes|on)$", os.environ.get(name, ""), re.IGNORECASE))


def lan_share_enabled() -> bool:
    """True when sharing the core off-localhost is opted in (mirrors web-client.ts)."""
    return _flag("SUTANDO_LAN_SHARE")


def tailnet_serve_enabled() -> bool:
    """True when `tailscale serve` fronts the webUI (HTTPS on the MagicDNS name)."""
    return _flag("SUTANDO_TAILNET_SERVE")


def is_private_lan_ipv4(ip: str) -> bool:
    """RFC1918 private-LAN IPv4 (10/8, 172.16/12, 192.168/16) — excludes
    loopback, link-local, and CGNAT/tailnet (100.64/10) like the TS original."""
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        m = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in m):
        return False
    if m[0] == 10:
        return True
    if m[0] == 172 and 16 <= m[1] <= 31:
        return True
    if m[0] == 192 and m[1] == 168:
        return True
    return False


def parse_ifconfig_lan_ipv4(text: str) -> str | None:
    """First private-LAN IPv4 from `ifconfig -a` output, interfaces in listed
    order (macOS lists them name-sorted enough for the TS parity goal: a
    deterministic pick). Pure for unit tests."""
    current: list[tuple[str, str]] = []
    iface = ""
    for line in text.splitlines():
        m = re.match(r"^(\S+?):", line)
        if m:
            iface = m.group(1)
            continue
        m = re.search(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and is_private_lan_ipv4(m.group(1)):
            current.append((iface, m.group(1)))
    if not current:
        return None
    return sorted(current, key=lambda t: t[0])[0][1]


def detect_lan_ipv4(run=None) -> str | None:
    """The machine's primary private-LAN IPv4 via ifconfig, or None. Injectable
    runner keeps it testable; any failure is a benign None."""
    if run is None:
        def run():  # pragma: no cover - thin subprocess shim
            try:
                r = subprocess.run(["ifconfig", "-a"], capture_output=True,
                                   timeout=1.5, text=True)
                return r.stdout if r.returncode == 0 and r.stdout else None
            except Exception:
                return None
    out = run()
    return parse_ifconfig_lan_ipv4(out) if out else None


def parse_tailnet_host(status) -> str | None:
    """Tailnet host from parsed `tailscale status --json`, or None. Prefers the
    MagicDNS name (trailing dot stripped) and falls back to the 100.x IPv4;
    nothing is advertised when the node reports Online: false."""
    if not isinstance(status, dict):
        return None
    self_ = status.get("Self")
    if not isinstance(self_, dict):
        return None
    if self_.get("Online") is False:
        return None
    dns = self_.get("DNSName")
    if isinstance(dns, str) and dns:
        return dns.rstrip(".")
    ips = self_.get("TailscaleIPs")
    if isinstance(ips, list):
        for ip in ips:
            if isinstance(ip, str) and ip.startswith("100."):
                return ip
    return None


def detect_tailnet_host(run=None) -> str | None:
    """Tailnet host via the local `tailscale` CLI, or None when tailscale is
    absent/down. Short timeout so a hung binary never blocks the launcher."""
    if run is None:
        def run():  # pragma: no cover - thin subprocess shim
            try:
                r = subprocess.run(["tailscale", "status", "--json"],
                                   capture_output=True, timeout=1.5, text=True)
                return r.stdout if r.returncode == 0 and r.stdout else None
            except Exception:
                return None
    out = run()
    if not out:
        return None
    try:
        return parse_tailnet_host(json.loads(out))
    except (ValueError, TypeError):
        return None


def compose_tailnet_url(host: str, serve: bool | None = None) -> str:
    """serve on → HTTPS on the MagicDNS name (tailscale serve fronts :443);
    off → plain HTTP on CLIENT_PORT."""
    if serve is None:
        serve = tailnet_serve_enabled()
    return f"https://{host}" if serve else f"http://{host}:{_client_port()}"


def tailnet_endpoint_url() -> str | None:
    if not lan_share_enabled():
        return None
    host = detect_tailnet_host()
    return compose_tailnet_url(host) if host else None


def lan_endpoint_url() -> str | None:
    if not lan_share_enabled():
        return None
    ip = detect_lan_ipv4()
    return f"http://{ip}:{_client_port()}" if ip else None


def compose_call_tiers() -> list[dict]:
    """Direct tiers in resolver-preferred order — identical shape + labels to
    composeCallTiers() in emit-call-tiers.ts."""
    tailnet = tailnet_endpoint_url()
    lan = lan_endpoint_url()
    return [
        {"tier": "direct-tailnet", "label": "Direct (Tailscale)",
         "url": tailnet, "reachable": tailnet is not None},
        {"tier": "direct-lan", "label": "Direct (LAN)",
         "url": lan, "reachable": lan is not None},
    ]


def emit_call_tiers(dest: str | None = None) -> str:
    """Write state/call-tiers.json ({ts, pid, call_tiers}) and return its path."""
    import time
    path = dest or str(status_path("call-tiers.json"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"ts": int(time.time()), "pid": os.getpid(),
               "call_tiers": compose_call_tiers()}
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


if __name__ == "__main__":
    print(f"call-tiers written: {emit_call_tiers()}")

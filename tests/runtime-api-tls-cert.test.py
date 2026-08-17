#!/usr/bin/env python3
"""Contract test for the SCP TLS sibling's self-signed certificate policy
(server.py _wss_ssl_context / _tls_san_list / _cert_covers).

Browsers ignore CN and reject SAN-less certs, and phones reach this host by
mDNS name or LAN IP — so the cert must carry a SAN naming every reachable
address, and must REGENERATE when the address set moves (DHCP re-lease,
hostname change) or when a pre-SAN legacy cert is found on disk.

Run: python3 tests/runtime-api-tls-cert.test.py   (needs openssl on PATH)
"""
from __future__ import annotations

import importlib.util
import os
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RA = REPO / "src" / "runtime-api"
sys.path.insert(0, str(RA))

FAILS: list = []


def check(cond: bool, label: str) -> None:
    print(("  ok " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def load_server_cls():
    spec = importlib.util.spec_from_file_location("ra_server", RA / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return next(v for v in vars(mod).values()
                if isinstance(v, type) and hasattr(v, "_wss_ssl_context"))


def gen(cert: Path, key: Path, san: "list[str] | None") -> None:
    args = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "825",
            "-subj", "/CN=sutando-server"]
    if san:
        args += ["-addext", "subjectAltName=" + ",".join(san)]
    subprocess.run(args, check=True, capture_output=True)


def main() -> int:
    cls = load_server_cls()

    san = cls._tls_san_list()
    check("DNS:localhost" in san and "IP:127.0.0.1" in san,
          "SAN always names localhost by DNS and IP")
    check(any(e.startswith("DNS:") and e.endswith(".local") for e in san),
          "SAN carries the mDNS .local name (stable across DHCP)")

    with tempfile.TemporaryDirectory() as td:
        cert, key = Path(td) / "cert.pem", Path(td) / "key.pem"

        gen(cert, key, san)
        check(cls._cert_covers(cert, san),
              "fresh SAN-ful cert covers its own SAN set")
        check(not cls._cert_covers(cert, san + ["IP:10.99.99.99"]),
              "an address not in the cert triggers regeneration")

        gen(cert, key, None)
        check(not cls._cert_covers(cert, san),
              "legacy CN-only cert (pre-SAN) triggers regeneration")

        check(not cls._cert_covers(Path(td) / "missing.pem", san),
              "missing cert triggers generation")

    # End-to-end through the real entry point: _wss_ssl_context generates
    # under state/auth/scp-tls and returns a loadable SSLContext; a second
    # call reuses the cert (no regeneration when SAN is unchanged).
    with tempfile.TemporaryDirectory() as td:
        inst = cls.__new__(cls)
        inst._state_dir = td
        os.environ["SUTANDO_SCP_WSS_TLS"] = "1"
        try:
            ctx = inst._wss_ssl_context()
            check(isinstance(ctx, ssl.SSLContext),
                  "_wss_ssl_context returns a loaded SSLContext")
            cert = Path(td) / "auth" / "scp-tls" / "cert.pem"
            key = Path(td) / "auth" / "scp-tls" / "key.pem"
            check(cert.exists() and key.exists(),
                  "cert + key generated under state/auth/scp-tls")
            check((key.stat().st_mode & 0o777) == 0o600,
                  "key file is 0600")
            mtime = cert.stat().st_mtime_ns
            inst._wss_ssl_context()
            check(cert.stat().st_mtime_ns == mtime,
                  "unchanged SAN set reuses the cert (no churn)")
            out = subprocess.run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-ext",
                 "subjectAltName"], check=True, capture_output=True,
                text=True).stdout
            check(all(e.split(":", 1)[1] in out for e in cls._tls_san_list()),
                  "generated cert's SAN extension names every address")
            os.environ["SUTANDO_SCP_WSS_TLS"] = "0"
            check(inst._wss_ssl_context() is None,
                  "TLS off → no context (plain-ws primary unaffected)")
        finally:
            os.environ.pop("SUTANDO_SCP_WSS_TLS", None)

    print(f"\n{'PASS — TLS cert policy green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())

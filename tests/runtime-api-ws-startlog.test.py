#!/usr/bin/env python3
"""The production start log names the wire it actually opened.

Pinned both polarities: start() with no TLS context must announce a cleartext
WebSocket at ws://, and with a TLS context a TLS WebSocket at wss:// — the
word "WSS" beside a ws:// URL is the exact operator-facing ambiguity this
guards against, so a cleartext start log containing "WSS" fails.

Run: python3 tests/runtime-api-ws-startlog.test.py   (needs aiohttp + openssl)
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from ws_transport import WsTransport  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok  " if cond else "FAIL") + " - " + msg)
    if not cond:
        FAILS.append(msg)


async def _start_and_log(ssl_context=None):
    logs = []
    t = WsTransport(dispatcher=None, token="t", log=logs.append,
                    host="127.0.0.1", port=0)
    await t.start(ssl_context=ssl_context)
    try:
        return logs[-1]
    finally:
        await t.cleanup()


def main():
    line = asyncio.run(_start_and_log(ssl_context=None))
    check("ws://" in line, f"plain start logs a ws:// URL: {line!r}")
    check("WSS" not in line,
          f"plain start log never says WSS beside ws://: {line!r}")
    check("cleartext" in line, f"plain start log says cleartext: {line!r}")

    with tempfile.TemporaryDirectory() as td:
        cert, key = Path(td) / "c.pem", Path(td) / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-keyout", str(key), "-out", str(cert),
                        "-days", "2", "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        line = asyncio.run(_start_and_log(ssl_context=ctx))
    check("wss://" in line, f"TLS start logs a wss:// URL: {line!r}")
    check("TLS" in line, f"TLS start log names TLS: {line!r}")

    print(f"\n{'PASS' if not FAILS else 'FAIL'} "
          f"({5 - len(FAILS)}/5)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

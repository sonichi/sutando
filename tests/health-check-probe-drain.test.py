#!/usr/bin/env python3
"""
Regression tests for check_port()'s probe-response drain.

The liveness probe used to read exactly one byte and close the socket.
Closing with unread bytes sends RST, so every probed server (agent-api,
dashboard, screen-capture) logged a BrokenPipeError traceback per health
run — 70 in agent-api.log by 2026-07-02, burying real regressions.

Guards:
  a) probing a healthy server yields status ok AND the server writes its
     full response without BrokenPipeError/ConnectionResetError
  b) a server that sends nothing is still detected as wedged (drain must
     not mask the wedged verdict)
  c) a server that EOFs after one byte is still ok (drain handles EOF)

Run: python3 tests/health-check-probe-drain.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok  " if cond else "FAIL") + " " + label)
    if not cond:
        failures.append(label)


def serve_once(behavior: str, server_errors: list[BaseException]):
    """One-shot TCP server on an ephemeral port. Returns (port, thread)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        try:
            conn.settimeout(5)
            conn.recv(4096)  # consume the probe request
            if behavior == "healthy":
                # Status line + headers first, then the body in later writes
                # — the shape socketserver-based handlers produce. The pre-fix
                # prober read ONE byte and closed, leaving the rest of chunk 1
                # unread; that close sends RST, so a later write raises
                # BrokenPipeError/ConnectionResetError (the original bug).
                conn.sendall(
                    b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n"
                    b"Content-Length: 16384\r\n\r\n"
                )
                time.sleep(0.4)
                conn.sendall(b"x" * 8192)
                time.sleep(0.1)
                conn.sendall(b"x" * 8192)
            elif behavior == "one-byte-eof":
                conn.sendall(b"H")
            elif behavior == "silent":
                time.sleep(12)  # longer than the 10s probe timeout
        except BaseException as e:  # noqa: BLE001 — the assertion target
            server_errors.append(e)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, t


def main() -> int:
    # a) healthy server: ok verdict, no broken pipe on the server side
    errs: list[BaseException] = []
    port, t = serve_once("healthy", errs)
    r = hc.check_port(port, "probe-drain-test", probe=True)
    t.join(timeout=10)
    check(r["status"] == "ok", "healthy server → ok")
    pipe_errs = [e for e in errs if isinstance(e, (BrokenPipeError, ConnectionResetError))]
    check(not pipe_errs, f"server wrote full response without reset (errors: {pipe_errs!r})")

    # b) wedged server still detected (drain must not mask the verdict)
    errs2: list[BaseException] = []
    port2, _t2 = serve_once("silent", errs2)
    r2 = hc.check_port(port2, "probe-drain-test", probe=True)
    check(r2["status"] == "wedged", "silent server → wedged")

    # c) server EOFing right after the first byte is still ok
    errs3: list[BaseException] = []
    port3, t3 = serve_once("one-byte-eof", errs3)
    r3 = hc.check_port(port3, "probe-drain-test", probe=True)
    t3.join(timeout=5)
    check(r3["status"] == "ok", "one-byte-then-EOF server → ok")

    print()
    if failures:
        print(f"{len(failures)} failure(s)")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

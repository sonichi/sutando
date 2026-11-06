#!/usr/bin/env python3
"""Hardened loopback server for local presentation drafts (owner spec 2026-08-26).

Serves ONE configured directory, read-only, to the trusted Presentation
panel's dev mode. Binding to 127.0.0.1 is not treated as trust: requests
need a random short-lived capability in the path, the Host header must be
loopback (DNS-rebinding defense), only GET/HEAD exist, and every response
carries nosniff + a sandboxing CSP. No directory listing, no write route,
no generic filesystem access — the served root is the whole surface.

Usage:
  python3 serve.py --root <dir> [--port 8899] [--ttl 3600]
Prints the capability URL on stdout; share it with the panel's dev setting.
"""
from __future__ import annotations

import argparse
import hmac
import mimetypes
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

# Explicit core types; mimetypes fills the long tail. HTML gets the sandbox
# CSP below — the deck must stay inert even if opened outside the panel.
_MIME = {".html": "text/html", ".htm": "text/html", ".css": "text/css",
         ".js": "text/javascript", ".mjs": "text/javascript",
         ".json": "application/json", ".txt": "text/plain",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
         ".woff": "font/woff", ".woff2": "font/woff2"}
_CSP = "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data:; connect-src 'none'"


def make_handler(root: Path, capability: str, port: int, expires_at: float):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}",
                     "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalWorkspace/1"

        def _deny(self, code: int, msg: str) -> None:
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve(self, head_only: bool) -> None:
            if (self.headers.get("Host") or "") not in allowed_hosts:
                return self._deny(403, "bad host")
            if time.time() > expires_at:
                return self._deny(403, "capability expired — restart the server")
            # Decode percent-encoding exactly once (never recursively) BEFORE
            # the checks, so %2e%2e/..%2f are checked as the traversal they are.
            try:
                path = unquote(self.path.split("?", 1)[0], errors="strict")
            except UnicodeDecodeError:
                return self._deny(404, "not found")
            parts = path.lstrip("/").split("/", 1)
            if not hmac.compare_digest(parts[0], capability):
                return self._deny(404, "not found")
            rel = parts[1] if len(parts) > 1 and parts[1] else "index.html"
            if "\x00" in rel or ".." in rel.split("/"):
                return self._deny(404, "not found")
            target = (root / rel).resolve()
            # resolve() also collapses symlink escapes; containment is the gate
            if root not in target.parents and target != root:
                return self._deny(404, "not found")
            if not target.is_file():
                return self._deny(404, "not found")
            data = target.read_bytes()
            ext = target.suffix.lower()
            ctype = _MIME.get(ext) or mimetypes.guess_type(target.name)[0] \
                or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype == "text/html":
                self.send_header("Content-Security-Policy", _CSP)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._serve(head_only=False)

        def do_HEAD(self):  # noqa: N802
            self._serve(head_only=True)

        def __getattr__(self, name):
            # Any other method (do_POST, do_PUT, ...) -> 405, never a write.
            if name.startswith("do_"):
                return lambda: self._deny(405, "read-only server")
            raise AttributeError(name)

        def log_message(self, fmt, *args):
            print(f"[local-workspace-server] {self.address_string()} "
                  f"{fmt % args}", flush=True)

    return Handler


def build_server(root: Path, port: int, ttl: int,
                 capability: "str | None" = None):
    root = root.resolve()
    if not root.is_dir():
        raise SystemExit(f"--root is not a directory: {root}")
    cap = capability or secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(root, cap, port, time.time() + ttl))
    # port=0 lets the OS choose; the Host allow-list must use the BOUND port.
    srv.RequestHandlerClass = make_handler(
        root, cap, srv.server_address[1], time.time() + ttl)
    return srv, cap


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--ttl", type=int, default=3600,
                    help="capability lifetime in seconds (default 1h)")
    args = ap.parse_args()
    srv, cap = build_server(args.root, args.port, args.ttl)
    actual_port = srv.server_address[1]
    print(f"[local-workspace-server] serving {args.root.resolve()} "
          f"(read-only, loopback, ttl={args.ttl}s)", flush=True)
    print(f"http://127.0.0.1:{actual_port}/{cap}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

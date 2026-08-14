#!/usr/bin/env python3
"""Tests for the X-Sutando-Capture-Token auth check in src/screen-capture-server.py.

Regression coverage for PR #1957: the /capture endpoint now requires a per-startup
token in the X-Sutando-Capture-Token header so a browser page on loopback cannot
trigger a capture (CSRF guard — browsers cannot set custom headers on no-cors fetches
and cannot read local 0600 files).

These tests exercise the handler auth logic and _load_or_create_capture_token()
without starting an HTTP server. Subprocess calls (screencapture) are never reached.

Run: python3 tests/screen-capture-server-auth.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest.mock
from http.server import BaseHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "screen_capture_server", REPO / "src" / "screen-capture-server.py"
)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Fake request helper — drives Handler.do_GET() without a real socket
# ---------------------------------------------------------------------------

class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key, default)


class _FakeRequest:
    def __init__(self):
        self.makefile_data = b""

    def makefile(self, *_a, **_kw):
        return io.BytesIO(self.makefile_data)


class _FakeHandler(sc.Handler):
    """Subclass that stubs out socket I/O so we can call do_GET directly."""

    def __init__(self, path: str, token_header: str | None):
        # Skip BaseHTTPRequestHandler.__init__ (needs a real socket).
        self.path = path
        self.headers = _FakeHeaders()
        if token_header is not None:
            self.headers["X-Sutando-Capture-Token"] = token_header
        self._response_code: int | None = None
        self._response_headers: dict = {}
        self._body: bytes = b""
        self._buf = io.BytesIO()
        self.wfile = self._buf
        self.rfile = io.BytesIO()

    # Intercept BaseHTTPRequestHandler send methods
    def send_response(self, code, message=None):
        self._response_code = code

    def send_header(self, key, value):
        self._response_headers[key] = value

    def end_headers(self):
        pass

    def log_message(self, *_a):
        pass


def _run_auth(path: str, token_header: str | None, capture_token: str | None):
    """Run do_GET() up to the auth gate and return its captured response.

    Patches CAPTURE_TOKEN and stubs out everything past the auth check so we
    never attempt a real screencapture subprocess call.
    """
    handler = _FakeHandler(path, token_header)
    with unittest.mock.patch.object(sc, "CAPTURE_TOKEN", capture_token), \
         unittest.mock.patch("os.makedirs"), \
         unittest.mock.patch.object(sc, "_signal_seeing"), \
         unittest.mock.patch.object(sc, "_notify_capture"), \
         unittest.mock.patch("subprocess.run", side_effect=Exception("should not reach screencapture")):
        try:
            handler.do_GET()
        except Exception:
            pass
    return (
        handler._response_code,
        handler._response_code == 403,
        handler._response_headers,
        handler._buf.getvalue(),
    )


# ---------------------------------------------------------------------------
# Auth handler tests
# ---------------------------------------------------------------------------

# 1. Missing header → 403
code, is_403, headers, body = _run_auth("/capture", None, "secret-token")
ok("missing header → 403", is_403, f"got code={code}")
ok("403 uses JSON response contract", headers.get("Content-Type") == "application/json",
   f"got headers={headers}")
ok("403 returns stable payload", json.loads(body) == {"status": "error", "error": "forbidden"},
   f"got body={body!r}")

# 2. Empty header → 403
code, is_403, _, _ = _run_auth("/capture", "", "secret-token")
ok("empty header → 403", is_403, f"got code={code}")

# 3. Wrong token → 403
code, is_403, _, _ = _run_auth("/capture", "wrong-token", "secret-token")
ok("wrong token → 403", is_403, f"got code={code}")

# 4. Correct token → NOT 403 (auth passes, proceed to capture logic)
code, is_403, _, _ = _run_auth("/capture", "secret-token", "secret-token")
ok("correct token → not 403", not is_403, f"got code={code}")

# 5. No CAPTURE_TOKEN (None) → fail-closed, 403 (token-load failure must not open the gate)
code, is_403, _, _ = _run_auth("/capture", None, None)
ok("CAPTURE_TOKEN=None → 403 (fail-closed)", is_403, f"got code={code}")

# 6. Correct token with query params → still passes
code, is_403, _, _ = _run_auth("/capture?display=1&format=jpeg", "secret-token", "secret-token")
ok("correct token + query params → not 403", not is_403, f"got code={code}")

# 7. Timing-safe comparison: partial prefix match is rejected
code, is_403, _, _ = _run_auth("/capture", "secret-tok", "secret-token")
ok("partial token prefix → 403", is_403, f"got code={code}")

# 8. Non-capture paths don't hit the auth gate (e.g. /health)
code, is_403, _, _ = _run_auth("/health", None, "secret-token")
ok("non-/capture path → no 403", not is_403, f"got code={code}")

# ---------------------------------------------------------------------------
# _load_or_create_capture_token() tests
# ---------------------------------------------------------------------------

def test_load_creates_new_token_when_missing() -> None:
    with tempfile.TemporaryDirectory() as td:
        tok_path = os.path.join(td, "screen-capture-token")
        with unittest.mock.patch.object(sc, "_CAPTURE_TOKEN_PATH", tok_path):
            token = sc._load_or_create_capture_token()
        ok("creates token when missing", bool(token) and len(token) >= 32,
           f"got {token!r}")
        ok("token file created 0600",
           os.path.exists(tok_path) and (os.stat(tok_path).st_mode & 0o777) == 0o600,
           f"mode={oct(os.stat(tok_path).st_mode) if os.path.exists(tok_path) else 'missing'}")


def test_load_reuses_existing_valid_token() -> None:
    with tempfile.TemporaryDirectory() as td:
        tok_path = os.path.join(td, "screen-capture-token")
        existing = "existing-valid-token-32chars-xxxxxxxx"
        fd = os.open(tok_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, existing.encode())
        os.close(fd)
        with unittest.mock.patch.object(sc, "_CAPTURE_TOKEN_PATH", tok_path):
            token = sc._load_or_create_capture_token()
        ok("reuses existing valid token", token == existing, f"got {token!r}")


def test_load_rejects_wrong_permissions() -> None:
    with tempfile.TemporaryDirectory() as td:
        tok_path = os.path.join(td, "screen-capture-token")
        # Write with 0644 — world-readable, should be rejected
        with open(tok_path, "w") as f:
            f.write("insecure-token")
        os.chmod(tok_path, 0o644)
        with unittest.mock.patch.object(sc, "_CAPTURE_TOKEN_PATH", tok_path):
            token = sc._load_or_create_capture_token()
        # Should reject the 0644 file and try to create a new one (may fail due
        # to the unlink+recreate in the same dir — either way, old token is NOT returned)
        ok("rejects 0644 token", token != "insecure-token",
           f"got {token!r}")


test_load_creates_new_token_when_missing()
test_load_reuses_existing_valid_token()
test_load_rejects_wrong_permissions()

# ---------------------------------------------------------------------------
# Downscale budget tests
# ---------------------------------------------------------------------------

def test_downscale_invokes_sips_with_bounds() -> None:
    with tempfile.NamedTemporaryFile() as frame:
        with unittest.mock.patch.object(sc.subprocess, "run") as run:
            ok_result = sc._downscale_frame(frame.name, 1280, 60)
        ok("downscale succeeds when sips succeeds", ok_result)
        ok("downscale bounds reach sips", run.call_args.args[0] == [
            "sips", "--resampleHeightWidthMax", "1280", "-s", "format",
            "jpeg", "-s", "formatOptions", "60", frame.name,
        ], f"got {run.call_args.args[0] if run.call_args else None}")


def test_downscale_failure_only_allows_small_original() -> None:
    with tempfile.NamedTemporaryFile() as small, tempfile.NamedTemporaryFile() as large:
        small.write(b"x")
        small.flush()
        large.write(b"x" * (sc.DOWNSCALE_FAIL_MAX_BYTES + 1))
        large.flush()
        with unittest.mock.patch.object(sc.subprocess, "run", side_effect=RuntimeError("sips failed")):
            ok("downscale failure permits a small original", sc._downscale_frame(small.name, 1280, 60))
            ok("downscale failure rejects an over-budget original", not sc._downscale_frame(large.name, 1280, 60))
    with unittest.mock.patch.object(sc.subprocess, "run", side_effect=RuntimeError("sips failed")), \
         unittest.mock.patch.object(sc.os.path, "getsize", side_effect=OSError("stat failed")):
        ok("downscale failure rejects an unreadable original", not sc._downscale_frame("missing.jpg", 1280, 60))


def test_capture_downscale_options_and_failure_are_visible() -> None:
    handler = _FakeHandler("/capture?format=jpeg&maxdim=1280&quality=60&silent=true", "secret-token")
    with unittest.mock.patch.object(sc, "CAPTURE_TOKEN", "secret-token"), \
         unittest.mock.patch("os.makedirs"), \
         unittest.mock.patch("subprocess.run"), \
         unittest.mock.patch.object(sc, "_downscale_frame", return_value=True) as downscale:
        handler._handle_capture()
    ok("capture passes bounded JPEG options to downscale", downscale.call_args.args[1:] == (1280, 60),
       f"got {downscale.call_args.args if downscale.call_args else None}")
    ok("capture returns success after downscale", handler._response_code == 200,
       f"got code={handler._response_code}")

    failed = _FakeHandler("/capture?format=jpeg&maxdim=1280&quality=60&silent=true", "secret-token")
    with unittest.mock.patch.object(sc, "CAPTURE_TOKEN", "secret-token"), \
         unittest.mock.patch("os.makedirs"), \
         unittest.mock.patch("subprocess.run"), \
         unittest.mock.patch.object(sc, "_downscale_frame", return_value=False):
        failed._handle_capture()
    ok("capture rejects a frame that cannot meet the downscale budget", failed._response_code == 500,
       f"got code={failed._response_code}")
    ok("downscale budget failure has a stable error", json.loads(failed._buf.getvalue()) == {
        "status": "error", "error": "downscale failed and frame exceeds budget"
    }, f"got body={failed._buf.getvalue()!r}")


test_downscale_invokes_sips_with_bounds()
test_downscale_failure_only_allows_small_original()
test_capture_downscale_options_and_failure_are_visible()

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

total = _passed + _failed
print(f"\n{_passed}/{total} passed" + (f", {_failed} failed" if _failed else " ✓"))
sys.exit(0 if _failed == 0 else 1)

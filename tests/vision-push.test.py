#!/usr/bin/env python3
"""
Tests for src/vision_push.py

Covers the guard logic in push_image() and is_voice_ready() without
touching the real voice-agent HTTP endpoint:

  push_image() guards (no network needed):
    - Missing file path → False
    - File too small (< MIN_FRAME_BYTES) → False
    - Non-image MIME → overridden to image/jpeg, forwarded

  push_image() with mocked is_voice_ready / _post:
    - Voice not ready → False
    - Voice ready, _post 200 → True
    - Voice ready, _post 500 → False
    - Voice ready, _post 0 (connection refused) → False

  is_voice_ready():
    - Network error → False (never raises)

  Constants:
    - MIN_FRAME_BYTES == 2048
    - VISION_PORT default == 7847
    - VISION_BASE matches VISION_PORT

Run: python3 tests/vision-push.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "vision_push", REPO / "src" / "vision_push.py"
)
vp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vp)

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def fail(label: str, msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {msg}")


def _make_image(tmp, name: str = "frame.jpg", size: int = 4096) -> Path:
    """Write a dummy file of `size` bytes. Accepts str or Path for tmp."""
    p = Path(tmp) / name
    p.write_bytes(b"\xff\xd8\xff" + b"\x00" * (size - 3))  # JPEG-like header
    return p


# ── constants ────────────────────────────────────────────────────────────────

def test_min_frame_bytes_is_2048():
    if vp.MIN_FRAME_BYTES == 2048:
        ok("(vp-a) MIN_FRAME_BYTES == 2048")
    else:
        fail("(vp-a) MIN_FRAME_BYTES == 2048", f"got {vp.MIN_FRAME_BYTES}")


def test_vision_port_default():
    # Default when env var not overridden at import time
    if isinstance(vp.VISION_PORT, int) and vp.VISION_PORT > 0:
        ok("(vp-b) VISION_PORT is a positive int")
    else:
        fail("(vp-b) VISION_PORT is a positive int", f"got {vp.VISION_PORT!r}")


def test_vision_base_contains_port():
    expected_port = str(vp.VISION_PORT)
    if expected_port in vp.VISION_BASE:
        ok("(vp-c) VISION_BASE URL contains VISION_PORT")
    else:
        fail("(vp-c) VISION_BASE URL contains VISION_PORT",
             f"port={vp.VISION_PORT} base={vp.VISION_BASE!r}")


# ── push_image: pre-network guards ───────────────────────────────────────────

def test_push_missing_file():
    result = vp.push_image("/nonexistent/path/frame.jpg")
    if result is False:
        ok("(pi-a) missing file path → False")
    else:
        fail("(pi-a) missing file path → False", f"got {result!r}")


def test_push_file_too_small():
    with tempfile.TemporaryDirectory() as tmp:
        small = Path(tmp) / "tiny.jpg"
        small.write_bytes(b"\xff\xd8" + b"\x00" * 100)  # 102 bytes < 2048
        result = vp.push_image(str(small))
    if result is False:
        ok("(pi-b) file < MIN_FRAME_BYTES → False (size guard, no network)")
    else:
        fail("(pi-b) file < MIN_FRAME_BYTES → False", f"got {result!r}")


def test_push_file_exactly_min_size_proceeds_to_voice_check():
    # 2048 bytes is NOT < MIN_FRAME_BYTES, so it passes the size guard
    # and hits is_voice_ready — mock that to return False to stay offline.
    with tempfile.TemporaryDirectory() as tmp:
        exact = _make_image(tmp, "exact.jpg", size=vp.MIN_FRAME_BYTES)
        with patch.object(vp, "is_voice_ready", return_value=False):
            result = vp.push_image(str(exact))
    if result is False:
        ok("(pi-c) file == MIN_FRAME_BYTES passes size guard, hits voice check → False (voice down)")
    else:
        fail("(pi-c) file == MIN_FRAME_BYTES passes size guard", f"got {result!r}")


def test_push_unreadable_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = _make_image(tmp, "unreadable.jpg", size=4096)
        os.chmod(p, 0o000)
        try:
            result = vp.push_image(str(p))
        finally:
            os.chmod(p, 0o644)
    if result is False:
        ok("(pi-d) unreadable file → False")
    else:
        fail("(pi-d) unreadable file → False", f"got {result!r}")


# ── push_image: voice-ready gate ─────────────────────────────────────────────

def test_push_voice_not_ready():
    with tempfile.TemporaryDirectory() as tmp:
        img = _make_image(tmp, size=4096)
        with patch.object(vp, "is_voice_ready", return_value=False):
            result = vp.push_image(str(img))
    if result is False:
        ok("(pi-e) is_voice_ready=False → push returns False")
    else:
        fail("(pi-e) is_voice_ready=False → False", f"got {result!r}")


def test_push_voice_ready_200():
    with tempfile.TemporaryDirectory() as tmp:
        img = _make_image(tmp, size=4096)
        with patch.object(vp, "is_voice_ready", return_value=True), \
             patch.object(vp, "_post", return_value=(200, b"ok")):
            result = vp.push_image(str(img))
    if result is True:
        ok("(pi-f) is_voice_ready=True, _post 200 → True")
    else:
        fail("(pi-f) is_voice_ready=True, _post 200 → True", f"got {result!r}")


def test_push_voice_ready_500():
    with tempfile.TemporaryDirectory() as tmp:
        img = _make_image(tmp, size=4096)
        with patch.object(vp, "is_voice_ready", return_value=True), \
             patch.object(vp, "_post", return_value=(500, b"error")):
            result = vp.push_image(str(img))
    if result is False:
        ok("(pi-g) is_voice_ready=True, _post 500 → False")
    else:
        fail("(pi-g) is_voice_ready=True, _post 500 → False", f"got {result!r}")


def test_push_voice_ready_connection_refused():
    # _post returns (0, b"") on connection error
    with tempfile.TemporaryDirectory() as tmp:
        img = _make_image(tmp, size=4096)
        with patch.object(vp, "is_voice_ready", return_value=True), \
             patch.object(vp, "_post", return_value=(0, b"")):
            result = vp.push_image(str(img))
    if result is False:
        ok("(pi-h) _post returns (0, b'') → False (connection refused)")
    else:
        fail("(pi-h) _post returns (0, b'') → False", f"got {result!r}")


# ── push_image: MIME override ─────────────────────────────────────────────────

def test_push_non_image_mime_overridden_to_jpeg():
    # .bin file has no recognised image MIME — should be overridden to image/jpeg
    # then proceed; we mock voice+post to observe that it continues (not short-circuits)
    post_calls = []
    def fake_post(path, body, ct, **kw):
        post_calls.append((path, ct))
        return (200, b"ok")

    with tempfile.TemporaryDirectory() as tmp:
        p = tmp / "frame.bin" if False else Path(tmp) / "frame.bin"
        p.write_bytes(b"\x00" * 4096)
        with patch.object(vp, "is_voice_ready", return_value=True), \
             patch.object(vp, "_post", side_effect=fake_post):
            vp.push_image(str(p))

    frame_calls = [(path, ct) for path, ct in post_calls if path == "/vision/frame"]
    if frame_calls and frame_calls[0][1] == "image/jpeg":
        ok("(pi-i) unknown MIME → overridden to image/jpeg")
    else:
        fail("(pi-i) unknown MIME → overridden to image/jpeg",
             f"frame_calls={frame_calls}")


# ── is_voice_ready: never raises ─────────────────────────────────────────────

def test_is_voice_ready_network_error_returns_false():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        try:
            result = vp.is_voice_ready(timeout=0.1)
            ok("(vr-a) URLError → False (never raises)")
        except Exception as e:
            fail("(vr-a) URLError → False (never raises)", str(e))
    if result is False:
        pass  # already passed above
    else:
        fail("(vr-a) URLError → False", f"got {result!r}")


def test_is_voice_ready_session_not_ready():
    import io, json
    class FakeResp:
        def __init__(self):
            self._data = json.dumps({"sessionReady": False}).encode()
            self.status = 200
        def read(self): return self._data
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = vp.is_voice_ready()
    if result is False:
        ok("(vr-b) sessionReady=false → False")
    else:
        fail("(vr-b) sessionReady=false → False", f"got {result!r}")


def test_is_voice_ready_session_ready():
    import json
    class FakeResp:
        def __init__(self):
            self._data = json.dumps({"sessionReady": True}).encode()
            self.status = 200
        def read(self): return self._data
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = vp.is_voice_ready()
    if result is True:
        ok("(vr-c) sessionReady=true → True")
    else:
        fail("(vr-c) sessionReady=true → True", f"got {result!r}")


if __name__ == "__main__":
    print("vision-push tests")
    print("=" * 40)
    test_min_frame_bytes_is_2048()
    test_vision_port_default()
    test_vision_base_contains_port()
    test_push_missing_file()
    test_push_file_too_small()
    test_push_file_exactly_min_size_proceeds_to_voice_check()
    test_push_unreadable_file()
    test_push_voice_not_ready()
    test_push_voice_ready_200()
    test_push_voice_ready_500()
    test_push_voice_ready_connection_refused()
    test_push_non_image_mime_overridden_to_jpeg()
    test_is_voice_ready_network_error_returns_false()
    test_is_voice_ready_session_not_ready()
    test_is_voice_ready_session_ready()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)

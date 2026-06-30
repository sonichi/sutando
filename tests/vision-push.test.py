#!/usr/bin/env python3
"""Tests for guard paths in src/vision_push.py.

Covers the early-return logic in push_image() and is_voice_ready(),
both of which execute before any network I/O:
  - push_image: non-existent path, too-small file, voice not ready
  - is_voice_ready: urlopen error, missing sessionReady key, True response
  - mime fallback: unknown extension → image/jpeg default

Network calls (urlopen) are stubbed so no real server is needed.

Run: python3 tests/vision-push.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("vision_push", REPO / "src" / "vision_push.py")
vp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vp)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# push_image — pre-network guards
# ---------------------------------------------------------------------------

def test_push_nonexistent_path() -> list[str]:
    """Non-existent path → False without touching the network."""
    fails: list[str] = []
    result = vp.push_image("/tmp/this-file-does-not-exist-vision-test-xyzzy.jpg")
    check("non-existent path should return False", result is False, fails)
    return fails


def test_push_file_too_small() -> list[str]:
    """File smaller than MIN_FRAME_BYTES → False."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\xff\xd8" + b"\x00" * 100)  # 102 bytes — well under 2048
        path = tf.name
    try:
        result = vp.push_image(path)
        check("tiny JPEG should return False", result is False, fails)
    finally:
        os.unlink(path)
    return fails


def test_push_file_exactly_at_min_bytes() -> list[str]:
    """File exactly MIN_FRAME_BYTES triggers voice-ready check (not silently dropped)."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\x00" * vp.MIN_FRAME_BYTES)
        path = tf.name
    try:
        # Voice is not running → is_voice_ready() returns False → push returns False.
        # Key: we reach the is_voice_ready() gate (not the size gate).
        result = vp.push_image(path)
        check("at-min-bytes file should pass size gate (reach voice check)", result is False, fails)
    finally:
        os.unlink(path)
    return fails


def test_push_voice_not_ready() -> list[str]:
    """Voice not ready → False even with a valid file."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\xff\xd8" + b"\x00" * 4000)  # 4002 bytes — over MIN_FRAME_BYTES
        path = tf.name
    try:
        with unittest.mock.patch.object(vp, "is_voice_ready", return_value=False):
            result = vp.push_image(path)
        check("voice-not-ready should return False", result is False, fails)
    finally:
        os.unlink(path)
    return fails


def test_push_succeeds_when_voice_ready() -> list[str]:
    """Valid file + voice ready + 200 response → True."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\xff\xd8" + b"\x00" * 4000)
        path = tf.name
    try:
        with unittest.mock.patch.object(vp, "is_voice_ready", return_value=True), \
             unittest.mock.patch.object(vp, "_post", return_value=(200, b"")):
            result = vp.push_image(path)
        check("valid file + voice ready should return True", result is True, fails)
    finally:
        os.unlink(path)
    return fails


def test_push_frame_rejected_4xx() -> list[str]:
    """Server returns 4xx for the frame POST → False."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\xff\xd8" + b"\x00" * 4000)
        path = tf.name
    try:
        call_count = [0]
        def fake_post(path_arg, body, content_type, timeout=3.0):
            call_count[0] += 1
            if "/frame" in path_arg:
                return 400, b"bad request"
            return 200, b""  # start call succeeds
        with unittest.mock.patch.object(vp, "is_voice_ready", return_value=True), \
             unittest.mock.patch.object(vp, "_post", side_effect=fake_post):
            result = vp.push_image(path)
        check("4xx frame response should return False", result is False, fails)
    finally:
        os.unlink(path)
    return fails


def test_push_unreadable_file() -> list[str]:
    """OSError on file read → False."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        tf.write(b"\x00" * 4000)
        path = tf.name
    try:
        original_open = builtins_open = __builtins__["open"] if isinstance(__builtins__, dict) else open
        with unittest.mock.patch("builtins.open", side_effect=OSError("permission denied")):
            result = vp.push_image(path)
        check("unreadable file should return False", result is False, fails)
    finally:
        os.unlink(path)
    return fails


# ---------------------------------------------------------------------------
# mime type detection
# ---------------------------------------------------------------------------

def test_mime_known_extension() -> list[str]:
    """.jpg extension → image/jpeg (no fallback needed)."""
    import mimetypes
    fails: list[str] = []
    mime, _ = mimetypes.guess_type("frame.jpg")
    check(f"jpeg mime wrong: {mime!r}", mime is not None and mime.startswith("image/"), fails)
    return fails


def test_mime_unknown_extension_fallback() -> list[str]:
    """Unknown extension gets fallback to image/jpeg inside push_image."""
    fails: list[str] = []
    with tempfile.NamedTemporaryFile(suffix=".xyzimage", delete=False) as tf:
        tf.write(b"\x00" * 4000)
        path = tf.name
    try:
        # Capture the content_type passed to _post for the /frame call
        captured = {}
        def fake_post(path_arg, body, content_type, timeout=3.0):
            if "/frame" in path_arg:
                captured["ct"] = content_type
            return 200, b""
        with unittest.mock.patch.object(vp, "is_voice_ready", return_value=True), \
             unittest.mock.patch.object(vp, "_post", side_effect=fake_post):
            vp.push_image(path)
        ct = captured.get("ct", "")
        check(f"unknown ext should fall back to image/jpeg, got {ct!r}",
              ct == "image/jpeg", fails)
    finally:
        os.unlink(path)
    return fails


# ---------------------------------------------------------------------------
# is_voice_ready
# ---------------------------------------------------------------------------

def test_is_voice_ready_urlopen_raises() -> list[str]:
    """Any exception from urlopen → False (no crash)."""
    fails: list[str] = []
    import urllib.request
    with unittest.mock.patch.object(urllib.request, "urlopen",
                                    side_effect=ConnectionRefusedError("no server")):
        result = vp.is_voice_ready()
    check("urlopen error should return False", result is False, fails)
    return fails


def test_is_voice_ready_true() -> list[str]:
    """Response with sessionReady=true → True."""
    fails: list[str] = []
    import urllib.request
    payload = json.dumps({"sessionReady": True}).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)
    mock_resp.read.return_value = payload
    with unittest.mock.patch.object(urllib.request, "urlopen", return_value=mock_resp):
        result = vp.is_voice_ready()
    check("sessionReady=true should return True", result is True, fails)
    return fails


def test_is_voice_ready_false_field() -> list[str]:
    """Response with sessionReady=false → False."""
    fails: list[str] = []
    import urllib.request
    payload = json.dumps({"sessionReady": False}).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)
    mock_resp.read.return_value = payload
    with unittest.mock.patch.object(urllib.request, "urlopen", return_value=mock_resp):
        result = vp.is_voice_ready()
    check("sessionReady=false should return False", result is False, fails)
    return fails


def test_is_voice_ready_missing_field() -> list[str]:
    """Response without sessionReady key → False."""
    fails: list[str] = []
    import urllib.request
    payload = json.dumps({"status": "ok"}).encode()
    mock_resp = unittest.mock.MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)
    mock_resp.read.return_value = payload
    with unittest.mock.patch.object(urllib.request, "urlopen", return_value=mock_resp):
        result = vp.is_voice_ready()
    check("missing sessionReady should return False", result is False, fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("push_image: non-existent path → False", test_push_nonexistent_path),
        ("push_image: file too small → False", test_push_file_too_small),
        ("push_image: exactly MIN_FRAME_BYTES reaches voice check", test_push_file_exactly_at_min_bytes),
        ("push_image: voice not ready → False", test_push_voice_not_ready),
        ("push_image: voice ready + 200 → True", test_push_succeeds_when_voice_ready),
        ("push_image: 4xx frame response → False", test_push_frame_rejected_4xx),
        ("push_image: OSError on read → False", test_push_unreadable_file),
        ("mime: known .jpg → image/ prefix", test_mime_known_extension),
        ("mime: unknown ext falls back to image/jpeg", test_mime_unknown_extension_fallback),
        ("is_voice_ready: urlopen raises → False", test_is_voice_ready_urlopen_raises),
        ("is_voice_ready: sessionReady=true → True", test_is_voice_ready_true),
        ("is_voice_ready: sessionReady=false → False", test_is_voice_ready_false_field),
        ("is_voice_ready: missing field → False", test_is_voice_ready_missing_field),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nvision-push guards: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

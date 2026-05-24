#!/usr/bin/env python3
"""Tests for src/screen-capture-server.py — HTTP handler logic.

Run: python3 tests/screen-capture-server.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "src" / "screen-capture-server.py"


def _load_mod():
    """Load screen-capture-server without starting the HTTP server."""
    spec = importlib.util.spec_from_file_location("screen_capture_server", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("http.server.HTTPServer"):  # don't actually bind a port
        spec.loader.exec_module(mod)
    return mod


# Load once for the module-level constants
_mod = _load_mod()


class TestDisplayIndexCoercion(unittest.TestCase):
    """Display number coercion: digit-only, clamped to 1..9."""

    def _coerce(self, raw):
        """Replicate the inline coercion from do_GET."""
        display_raw = raw
        return (
            int(display_raw)
            if display_raw
            and display_raw.isdigit()
            and 1 <= int(display_raw) <= 9
            else None
        )

    def test_valid_display_1(self):
        self.assertEqual(self._coerce("1"), 1)

    def test_valid_display_9(self):
        self.assertEqual(self._coerce("9"), 9)

    def test_valid_display_mid(self):
        self.assertEqual(self._coerce("3"), 3)

    def test_zero_returns_none(self):
        self.assertIsNone(self._coerce("0"))

    def test_ten_returns_none(self):
        self.assertIsNone(self._coerce("10"))

    def test_negative_returns_none(self):
        self.assertIsNone(self._coerce("-1"))

    def test_non_digit_returns_none(self):
        self.assertIsNone(self._coerce("abc"))

    def test_empty_returns_none(self):
        self.assertIsNone(self._coerce(""))

    def test_none_returns_none(self):
        self.assertIsNone(self._coerce(None))

    def test_shell_injection_attempt_returns_none(self):
        self.assertIsNone(self._coerce("1; rm -rf /"))

    def test_float_string_returns_none(self):
        self.assertIsNone(self._coerce("2.5"))


class TestFormatValidation(unittest.TestCase):
    """Format parameter: png/jpg/jpeg accepted, everything else defaults to png."""

    def _validate(self, fmt):
        if fmt not in ("png", "jpg", "jpeg"):
            fmt = "png"
        return fmt

    def test_png_accepted(self):
        self.assertEqual(self._validate("png"), "png")

    def test_jpg_accepted(self):
        self.assertEqual(self._validate("jpg"), "jpg")

    def test_jpeg_accepted(self):
        self.assertEqual(self._validate("jpeg"), "jpeg")

    def test_bmp_defaults_to_png(self):
        self.assertEqual(self._validate("bmp"), "png")

    def test_empty_defaults_to_png(self):
        self.assertEqual(self._validate(""), "png")

    def test_gif_defaults_to_png(self):
        self.assertEqual(self._validate("gif"), "png")

    def test_path_traversal_defaults_to_png(self):
        self.assertEqual(self._validate("../etc/passwd"), "png")


class TestExtensionMapping(unittest.TestCase):
    """ext and type_flag derived from validated format."""

    def _ext_and_flag(self, fmt):
        ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
        type_flag = "jpg" if ext == "jpg" else "png"
        return ext, type_flag

    def test_png(self):
        self.assertEqual(self._ext_and_flag("png"), ("png", "png"))

    def test_jpg(self):
        self.assertEqual(self._ext_and_flag("jpg"), ("jpg", "jpg"))

    def test_jpeg_maps_to_jpg_ext(self):
        ext, flag = self._ext_and_flag("jpeg")
        self.assertEqual(ext, "jpg")
        self.assertEqual(flag, "jpg")


class TestModuleConstants(unittest.TestCase):
    def test_port_is_7845(self):
        self.assertEqual(_mod.PORT, 7845)

    def test_dir_is_tmp_sutando(self):
        self.assertIn("sutando", _mod.DIR)

    def test_notify_debounce_positive(self):
        self.assertGreater(_mod.NOTIFY_DEBOUNCE_S, 0)


class TestPingEndpoint(unittest.TestCase):
    """The /ping path returns 200 + JSON with pong:true."""

    def setUp(self):
        self.handler = _mod.Handler.__new__(_mod.Handler)
        self.buf = io.BytesIO()

        # Minimal wfile/send_response/send_header/end_headers mock
        self.handler.wfile = self.buf
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.path = "/ping"

    def test_ping_sends_200(self):
        self.handler.do_GET()
        self.handler.send_response.assert_called_once_with(200)

    def test_ping_body_contains_pong(self):
        self.handler.do_GET()
        body = self.buf.getvalue()
        data = json.loads(body)
        self.assertTrue(data.get("pong"))


class TestCaptureEndpointBadPath(unittest.TestCase):
    """Unknown paths return 404."""

    def setUp(self):
        self.handler = _mod.Handler.__new__(_mod.Handler)
        self.buf = io.BytesIO()
        self.handler.wfile = self.buf
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.path = "/unknown"

    def test_unknown_path_returns_404(self):
        self.handler.do_GET()
        self.handler.send_response.assert_called_once_with(404)


if __name__ == "__main__":
    unittest.main()

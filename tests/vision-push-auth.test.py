#!/usr/bin/env python3
"""Vision push requests carry the shared local API bearer."""

from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import vision_push  # noqa: E402


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        return 200

    def read(self):
        return b'{"status":"ok"}'


class VisionPushAuthTest(unittest.TestCase):
    def test_post_sends_local_api_bearer(self):
        captured = {}

        def urlopen(req, timeout):
            captured["request"] = req
            captured["timeout"] = timeout
            return Response()

        with (
            mock.patch.object(vision_push, "resolve_workspace", return_value=ROOT / "workspace"),
            mock.patch.object(vision_push, "local_api_token", return_value="owner-token") as token,
            mock.patch("urllib.request.urlopen", side_effect=urlopen),
        ):
            status, body = vision_push._post("/vision/start", b"{}", "application/json")

        self.assertEqual((status, body), (200, b'{"status":"ok"}'))
        self.assertEqual(captured["request"].get_header("Authorization"), "Bearer owner-token")
        token.assert_called_once_with(ROOT / "workspace", "")

    def test_post_returns_zero_when_token_resolution_fails(self):
        def urlopen(_req, timeout):
            raise AssertionError("urlopen must not run without a token")

        with (
            mock.patch.object(vision_push, "resolve_workspace", return_value=ROOT / "workspace"),
            mock.patch.object(vision_push, "local_api_token", side_effect=RuntimeError("no token")),
            mock.patch("urllib.request.urlopen", side_effect=urlopen),
        ):
            status, body = vision_push._post("/vision/start", b"{}", "application/json")

        self.assertEqual((status, body), (0, b""))


if __name__ == "__main__":
    unittest.main()

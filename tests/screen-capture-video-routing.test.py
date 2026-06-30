#!/usr/bin/env python3
"""Regression guard: GET /capture-video records a video and is NOT swallowed by
the /capture screenshot branch.

## The bug

`do_GET` dispatched the screenshot branch on `self.path.startswith("/capture")`.
But "/capture-video" also startswith("/capture"), so a video request fell into
the screenshot handler and came back as a PNG instead of recording a .mov.
Caught self-testing before merge; this locks the routing so it can't regress.

The test mocks `screencapture` (creates a dummy file at the output path) so it
runs headless with no real screen recording.
"""

import http.server
import importlib.util
import json
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "screen-capture-server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("screen_capture_server", SRC)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    returncode = 0


def _fake_run(cmd, *args, **kwargs):
    # Emulate screencapture: write a non-empty file at the output path (last arg).
    out = Path(cmd[-1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00fakemediabytes")
    return FakeProc()


class TestCaptureVideoRouting(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        # Stub the recorder + silence the menu-bar/notify side-effects. Use
        # patch.object + addCleanup so the real implementations are restored
        # after each test — `self.mod.subprocess` is the SHARED stdlib module,
        # so a bare assignment would leak the fake into any later test in the
        # same process (caught in review).
        for target, repl in (("subprocess", None), ("_signal_seeing", lambda: None),
                             ("_notify_capture", lambda: None)):
            if target == "subprocess":
                p = mock.patch.object(self.mod.subprocess, "run", _fake_run)
            else:
                p = mock.patch.object(self.mod, target, repl)
            p.start()
            self.addCleanup(p.stop)
        # Deterministic token so /capture-video auth is testable. Fresh module
        # per test, so this assignment doesn't leak.
        self.token = "test-capture-token"
        self.mod.CAPTURE_VIDEO_TOKEN = self.token
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get(self, path, token=None):
        req = urllib.request.Request(self.base + path)
        if token is not None:
            req.add_header("X-Sutando-Capture-Token", token)
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())

    def test_capture_video_returns_mov(self):
        # The whole point: /capture-video is NOT intercepted by /capture.
        status, body = self._get("/capture-video?seconds=1&silent=true", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["path"].endswith(".mov"),
                        f"/capture-video must record a .mov, got {body['path']}")
        self.assertEqual(body["seconds"], 1)

    def test_capture_still_returns_png(self):
        # The screenshot branch still works for the plain /capture path.
        status, body = self._get("/capture?silent=true")
        self.assertEqual(status, 200)
        self.assertTrue(body["path"].endswith(".png"),
                        f"/capture must return a .png, got {body['path']}")

    def test_duration_is_clamped(self):
        # Out-of-range / non-numeric seconds falls back to the 5s default.
        _, body = self._get("/capture-video?seconds=999&silent=true", token=self.token)
        self.assertEqual(body["seconds"], 5)
        _, body = self._get("/capture-video?seconds=abc&silent=true", token=self.token)
        self.assertEqual(body["seconds"], 5)

    def test_capture_video_requires_token(self):
        # Drive-by defense: no token / wrong token -> 403, no recording.
        for bad in (None, "wrong-token"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("/capture-video?seconds=1&silent=true", token=bad)
            self.assertEqual(ctx.exception.code, 403)
            ctx.exception.close()

    def test_routing_guard_present_in_source(self):
        # Structural backstop: the /capture branch must exclude /capture-video.
        src = SRC.read_text()
        self.assertIn('not self.path.startswith("/capture-video")', src,
                      "the /capture screenshot branch must exclude /capture-video")


if __name__ == "__main__":
    unittest.main()

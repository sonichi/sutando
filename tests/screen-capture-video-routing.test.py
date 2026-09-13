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


def _fake_platform_capture(path, fmt="png"):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00fakemediabytes")
    return True


class FakePopen:
    """Emulate an open-ended `screencapture -v <path>` for the toggle path:
    the .mov is finalized (written) when the process is SIGINT'd on stop."""

    def __init__(self, cmd, *args, **kwargs):
        self._out = Path(cmd[-1])

    def send_signal(self, sig):
        self._out.parent.mkdir(parents=True, exist_ok=True)
        self._out.write_bytes(b"\x00fakemediabytes")

    def wait(self, timeout=None):
        return 0


class FailingStopProc:
    def send_signal(self, sig):
        pass

    def wait(self, timeout=None):
        raise RuntimeError("stop failed")


class EmptyStopProc:
    def send_signal(self, sig):
        pass

    def wait(self, timeout=None):
        return 0


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
        # Toggle path spawns via Popen (not run) — mock it too, same shared-module
        # care (patch.object + addCleanup so it doesn't leak into other tests).
        pp = mock.patch.object(self.mod.subprocess, "Popen", FakePopen)
        pp.start()
        self.addCleanup(pp.stop)
        platform = mock.patch.object(self.mod, "is_macos", return_value=True)
        platform.start()
        self.addCleanup(platform.stop)
        capture = mock.patch.object(
            self.mod, "_platform_capture_screen", _fake_platform_capture
        )
        capture.start()
        self.addCleanup(capture.stop)
        # Deterministic token so /capture and /capture-video auth is testable.
        # Fresh module per test, so this assignment doesn't leak.
        self.token = "test-capture-token"
        self.mod.CAPTURE_TOKEN = self.token
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

    def test_capture_video_requires_action(self):
        # Toggle-only: no action (the old fixed-duration path) → 400, no recording.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/capture-video?silent=true", token=self.token)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_dispatch_delegates_capture_routes(self):
        # Keep do_GET as routing only; endpoint behavior belongs to the named
        # handlers where each path can evolve without growing one shared block.
        handler = object.__new__(self.mod.Handler)
        with mock.patch.object(self.mod.Handler, "_handle_capture") as still, \
             mock.patch.object(self.mod.Handler, "_handle_capture_video") as video:
            handler.path = "/capture?silent=true"
            handler.do_GET()
            still.assert_called_once_with()
            video.assert_not_called()

            still.reset_mock()
            handler.path = "/capture-video?action=start&silent=true"
            handler.do_GET()
            video.assert_called_once_with()
            still.assert_not_called()

    def test_ping_route_keeps_wire_payload(self):
        status, body = self._get("/ping")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"pong": True})

    def test_capture_still_returns_png(self):
        # The screenshot branch still works for the plain /capture path.
        status, body = self._get("/capture?silent=true", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["path"].endswith(".png"),
                        f"/capture must return a .png, got {body['path']}")

    def test_capture_all_displays_returns_every_path(self):
        status, body = self._get("/capture?all=true&silent=true", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["displays"], 4)
        self.assertEqual(len(body["all_paths"]), 4)

    def test_capture_all_stops_at_first_missing_display(self):
        calls = 0

        def run_until_missing(cmd, *args, **kwargs):
            nonlocal calls
            calls += 1
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            if calls == 1:
                out.write_bytes(b"\x00fakemediabytes")
                return type("Result", (), {"returncode": 0})()
            out.write_bytes(b"")
            return type("Result", (), {"returncode": 1})()

        with mock.patch.object(self.mod.subprocess, "run", run_until_missing):
            status, body = self._get(
                "/capture?all=true&format=invalid&silent=true", token=self.token
            )
        self.assertEqual(status, 200)
        self.assertEqual(calls, 2)
        self.assertNotIn("all_paths", body)
        self.assertTrue(body["path"].endswith(".png"))

    def test_capture_video_requires_token(self):
        # Drive-by defense: no token / wrong token -> 403, no recording.
        for bad in (None, "wrong-token"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("/capture-video?action=start&silent=true", token=bad)
            self.assertEqual(ctx.exception.code, 403)
            ctx.exception.close()

    def test_toggle_start_then_stop_drops_clip(self):
        # ⌃R press 1 → recording (non-blocking); press 2 → ok + the .mov path.
        status, body = self._get("/capture-video?action=start&silent=true", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "recording")
        self.assertTrue(body["path"].endswith(".mov"))
        path = body["path"]
        status, body = self._get("/capture-video?action=stop&silent=true", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["path"], path)
        self.assertTrue(Path(path).exists() and Path(path).stat().st_size > 0)

    def test_toggle_non_silent_display_signals_and_records(self):
        with mock.patch.object(self.mod, "_signal_seeing") as signal_seeing, \
             mock.patch.object(self.mod, "_notify_capture") as notify_capture:
            status, body = self._get(
                "/capture-video?action=start&display=2&audio=off", token=self.token
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "recording")
            status, body = self._get("/capture-video?action=stop", token=self.token)
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "ok")
        self.assertEqual(signal_seeing.call_count, 2)
        self.assertEqual(notify_capture.call_count, 2)

    def test_toggle_stop_when_idle(self):
        # Stop with nothing recording is a harmless no-op, not an error.
        _, body = self._get("/capture-video?action=stop&silent=true", token=self.token)
        self.assertEqual(body["status"], "idle")

    def test_toggle_double_start_is_guarded(self):
        _, b1 = self._get("/capture-video?action=start&silent=true", token=self.token)
        self.assertEqual(b1["status"], "recording")
        _, b2 = self._get("/capture-video?action=start&silent=true", token=self.token)
        self.assertEqual(b2["status"], "already_recording")
        self.assertEqual(b2["path"], b1["path"])
        self._get("/capture-video?action=stop&silent=true", token=self.token)  # cleanup

    def test_toggle_start_requires_token(self):
        # Token gate applies to the toggle actions too — no drive-by start.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/capture-video?action=start&silent=true", token="wrong-token")
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_toggle_start_failure_uses_json_error_contract(self):
        with mock.patch.object(self.mod.subprocess, "Popen", side_effect=RuntimeError("start failed")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("/capture-video?action=start&silent=true", token=self.token)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(ctx.exception.headers.get_content_type(), "application/json")
        self.assertEqual(
            json.loads(ctx.exception.read()),
            {"status": "error", "error": "start failed"},
        )
        ctx.exception.close()

    def test_toggle_stop_failure_uses_json_error_contract(self):
        self.mod._active_recording = {
            "proc": FailingStopProc(),
            "path": "/unused/failed-recording.mov",
            "watchdog": None,
        }
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/capture-video?action=stop&silent=true", token=self.token)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(ctx.exception.headers.get_content_type(), "application/json")
        self.assertEqual(
            json.loads(ctx.exception.read()),
            {"status": "error", "error": "stop failed"},
        )
        ctx.exception.close()

    def test_toggle_stop_rejects_empty_recording(self):
        self.mod._active_recording = {
            "proc": EmptyStopProc(),
            "path": "/unused/empty-recording.mov",
            "watchdog": None,
        }
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/capture-video?action=stop&silent=true", token=self.token)
        self.assertEqual(ctx.exception.code, 500)
        self.assertEqual(
            json.loads(ctx.exception.read()),
            {"status": "error", "error": "recording produced no file"},
        )
        ctx.exception.close()

    def test_routing_guard_present_in_source(self):
        # Structural backstop: the /capture branch must exclude /capture-video.
        src = SRC.read_text()
        self.assertIn('not self.path.startswith("/capture-video")', src,
                      "the /capture screenshot branch must exclude /capture-video")


if __name__ == "__main__":
    unittest.main()

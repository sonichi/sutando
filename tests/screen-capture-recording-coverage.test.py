#!/usr/bin/env python3
"""Coverage for the meeting-length recording path in src/screen-capture-server.py.

Addresses the diff-coverage gate + Rui's CR (cover notify on/off + start/stop/
auto-stop call sites). Exercises:
  - _post_recording_state(on) — the notifyutil on/off push (lines 96-97),
  - GET /capture-video?action=start&max=<N> — the ?max= safety-cap parse (line 342)
    + notify-on (347),
  - the watchdog _auto_stop callback — notify-off (line 328).

No real screen recording or notifications: subprocess.Popen (screencapture +
notifyutil) and threading.Timer are mocked, so it runs headless.
"""
import http.server
import importlib.util
import threading
import unittest
import urllib.request
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "screen-capture-server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("screen_capture_server", SRC)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakePopen:
    """Records every command; for screencapture, writes a dummy output file."""
    calls: list = []

    def __init__(self, cmd, *a, **k):
        FakePopen.calls.append(list(cmd))
        if cmd and cmd[0] == "screencapture":
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")

    def send_signal(self, *a):
        pass

    def wait(self, *a, **k):
        return 0


class CaptureTimer:
    """Captures the watchdog (interval, callback) instead of arming a real timer."""
    captured: list = []
    daemon = True

    def __init__(self, interval, fn):
        self.interval = interval
        self.fn = fn
        CaptureTimer.captured.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


def _notifies(calls):
    return [" ".join(c) for c in calls if c and c[0] == "notifyutil"]


class RecordingCoverage(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.mod.CAPTURE_TOKEN = "test-capture-token"
        FakePopen.calls = []
        CaptureTimer.captured = []
        self.pp = mock.patch.object(self.mod.subprocess, "Popen", FakePopen)
        self.pt = mock.patch.object(self.mod.threading, "Timer", CaptureTimer)
        self.pp.start()
        self.pt.start()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.mod.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.pt.stop()
        self.pp.stop()

    def test_post_recording_state_on_off(self):
        # Direct: lines 96-97 (notifyutil push for both on and off).
        self.mod._post_recording_state(True)
        self.mod._post_recording_state(False)
        joined = _notifies(FakePopen.calls)
        self.assertTrue(any("com.sutando.recording.on" in n for n in joined))
        self.assertTrue(any("com.sutando.recording.off" in n for n in joined))

    def test_max_cap_parse_and_watchdog_auto_stop(self):
        # Start a meeting-length recording with ?max=5 -> covers line 342 (cap parse)
        # and 347 (notify on).
        url = f"http://127.0.0.1:{self.port}/capture-video?action=start&max=5&silent=true"
        req = urllib.request.Request(url, headers={"X-Sutando-Capture-Token": "test-capture-token"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(CaptureTimer.captured, "watchdog Timer was not created")
        self.assertEqual(CaptureTimer.captured[-1].interval, 5,
                         "?max=5 should set the watchdog cap to 5s (line 342)")
        self.assertTrue(any("com.sutando.recording.on" in n for n in _notifies(FakePopen.calls)),
                        "recording start should post notify-on (347)")

        # Fire the watchdog auto-stop callback -> line 328 (_post_recording_state(False)).
        FakePopen.calls = []
        CaptureTimer.captured[-1].fn()
        self.assertTrue(any("com.sutando.recording.off" in n for n in _notifies(FakePopen.calls)),
                        "watchdog auto-stop should post notify-off (328)")

    def test_max_cap_bounded_at_4h(self):
        # A too-large ?max is clamped to 4h (also exercises line 342's min()).
        url = f"http://127.0.0.1:{self.port}/capture-video?action=start&max=99999&silent=true"
        req = urllib.request.Request(url, headers={"X-Sutando-Capture-Token": "test-capture-token"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(CaptureTimer.captured[-1].interval, 4 * 3600)

    def test_request_stop_posts_notify_off(self):
        # Start, then explicit action=stop -> covers line 265 (_post_recording_state(False)
        # on the request-stop path). FakePopen wrote the clip file so the size check passes.
        hdr = {"X-Sutando-Capture-Token": "test-capture-token"}
        base = f"http://127.0.0.1:{self.port}/capture-video"
        with urllib.request.urlopen(urllib.request.Request(base + "?action=start&silent=true", headers=hdr), timeout=5) as r:
            self.assertEqual(r.status, 200)
        FakePopen.calls = []
        with urllib.request.urlopen(urllib.request.Request(base + "?action=stop&silent=true", headers=hdr), timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(any("com.sutando.recording.off" in n for n in _notifies(FakePopen.calls)),
                        "request-stop should post notify-off (265)")

    def test_notify_swallows_popen_error(self):
        # notifyutil push is fire-and-forget: a Popen failure must be swallowed,
        # never bubble into the recording path (lines 96-97, the except: pass).
        def boom(*a, **k):
            raise OSError("notifyutil not found")
        with mock.patch.object(self.mod.subprocess, "Popen", boom):
            self.mod._post_recording_state(True)  # must not raise


if __name__ == "__main__":
    unittest.main()

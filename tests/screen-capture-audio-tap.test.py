#!/usr/bin/env python3
"""Coverage for the process-tap audio pipeline in src/screen-capture-server.py
(issue #2314, diff-coverage gate).

Exercises:
  - _ffmpeg() — PATH hit, homebrew fallback, and not-found,
  - _spawn_audio_captures() — tap alive (mix/system), tap dead → legacy
    fallback, tap unbuildable → legacy fallback,
  - _finalize_recording() — mux with sys+mic, sys-only, no-audio rename,
    mux-failure rename, legacy passthrough,
  - GET /capture-video?action=start — audio=mix registers tap+mic and records
    video to *-video.mov; `on` aliases to mix; audio=mic keeps the legacy -g
    flag; tap-fallback also lands on -g,
  - GET /capture-video?action=stop — returns the muxed final path.

No real audio, screencapture, swiftc, or ffmpeg: subprocess and the tap
binary checks are mocked, so it runs headless.
"""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "screen-capture-server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("screen_capture_server", SRC)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    """A capture process that stays alive until signalled."""
    def __init__(self, cmd, *a, **k):
        self.cmd = list(cmd)
        self._alive = True
        if cmd and cmd[0] == "screencapture":
            out = Path(cmd[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"v" * 100)

    def poll(self):
        return None if self._alive else 0

    def send_signal(self, *_):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, *a, **k):
        self._alive = False
        return 0


class DeadProc(FakeProc):
    """A tap that dies immediately (TCC denied)."""
    def poll(self):
        return 1


class RaisingProc(FakeProc):
    """A capture process whose send_signal raises."""
    def send_signal(self, *_):
        raise OSError("no such process")


class TestEnsureTapBinary(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp()
        self.mod.TAP_BIN = os.path.join(self.tmp, "sys-audio-tap")
        self.mod.TAP_BUILD = os.path.join(self.tmp, "build-audio-tap.sh")

    def test_already_built(self):
        Path(self.mod.TAP_BIN).write_bytes(b"x")
        self.assertTrue(self.mod._ensure_tap_binary())

    def test_build_produces_binary(self):
        def fake_run(cmd, **k):
            Path(self.mod.TAP_BIN).write_bytes(b"built")
            return mock.Mock(returncode=0)
        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            self.assertTrue(self.mod._ensure_tap_binary())

    def test_build_leaves_binary_missing(self):
        with mock.patch.object(self.mod.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(self.mod._ensure_tap_binary())

    def test_build_raises(self):
        with mock.patch.object(self.mod.subprocess, "run", side_effect=OSError("no bash")):
            self.assertFalse(self.mod._ensure_tap_binary())


class TestFfmpegResolver(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()

    def test_path_hit(self):
        with mock.patch("shutil.which", return_value="/somewhere/ffmpeg"):
            self.assertEqual(self.mod._ffmpeg(), "/somewhere/ffmpeg")

    def test_homebrew_fallback(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(self.mod.os.path, "exists",
                               side_effect=lambda p: p == "/opt/homebrew/bin/ffmpeg"):
            self.assertEqual(self.mod._ffmpeg(), "/opt/homebrew/bin/ffmpeg")

    def test_not_found(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(self.mod.os.path, "exists", return_value=False):
            self.assertIsNone(self.mod._ffmpeg())


class TestSpawnAudioCaptures(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "clip-x")
        # make the wait() no-op so tests don't sleep 0.7s per spawn
        self.wait_patch = mock.patch.object(threading.Event, "wait", lambda *_a, **_k: None)
        self.wait_patch.start()

    def tearDown(self):
        self.wait_patch.stop()

    def test_mix_tap_and_mic(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "Popen", FakeProc):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNotNone(tap)
        self.assertIsNotNone(mic)
        self.assertFalse(fallback)
        self.assertEqual(tap.cmd[-1], self.base + "-sys.wav")
        self.assertEqual(mic.cmd[0], "/ff/ffmpeg")
        self.assertEqual(mic.cmd[-1], self.base + "-mic.wav")

    def test_system_only_no_mic(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod.subprocess, "Popen", FakeProc):
            tap, mic, fallback = self.mod._spawn_audio_captures("system", self.base)
        self.assertIsNotNone(tap)
        self.assertIsNone(mic)
        self.assertFalse(fallback)

    def test_tap_dead_falls_back(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod.subprocess, "Popen", DeadProc):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNone(tap)
        self.assertIsNone(mic)
        self.assertTrue(fallback)

    def test_unbuildable_falls_back(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=False):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNone(tap)
        self.assertIsNone(mic)
        self.assertTrue(fallback)

    def test_tap_spawn_raises_falls_back(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod.subprocess, "Popen", side_effect=OSError("nope")):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNone(tap)
        self.assertIsNone(mic)
        self.assertTrue(fallback)

    def test_mix_no_ffmpeg_leaves_mic_none(self):
        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod, "_ffmpeg", return_value=None), \
             mock.patch.object(self.mod.subprocess, "Popen", FakeProc):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNotNone(tap)  # tap itself is fine — only mic path failed
        self.assertIsNone(mic)
        self.assertFalse(fallback)  # tap alone still counts as success

    def test_mic_dies_instantly_leaves_mic_none(self):
        # tap must survive (FakeProc), only the mic (ffmpeg) proc dies
        calls = {"n": 0}

        def popen_side_effect(cmd, **k):
            calls["n"] += 1
            return FakeProc(cmd) if calls["n"] == 1 else DeadProc(cmd)

        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "Popen", side_effect=popen_side_effect):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNotNone(tap)
        self.assertIsNone(mic)
        self.assertFalse(fallback)

    def test_mic_spawn_raises_leaves_mic_none(self):
        calls = {"n": 0}

        def popen_side_effect(cmd, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeProc(cmd)
            raise OSError("no ffmpeg binary")

        with mock.patch.object(self.mod, "_ensure_tap_binary", return_value=True), \
             mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "Popen", side_effect=popen_side_effect):
            tap, mic, fallback = self.mod._spawn_audio_captures("mix", self.base)
        self.assertIsNotNone(tap)
        self.assertIsNone(mic)
        self.assertFalse(fallback)


class TestFinalizeRecording(unittest.TestCase):
    def setUp(self):
        self.mod = load_module()
        self.tmp = tempfile.mkdtemp()
        self.final = os.path.join(self.tmp, "clip-y.mov")
        self.video = os.path.join(self.tmp, "clip-y-video.mov")

    def _rec(self, **over):
        rec = {"proc": FakeProc(["screencapture", self.video]),
               "tap": None, "mic": None,
               "path": self.final, "video_path": self.video}
        rec.update(over)
        return rec

    def test_legacy_passthrough(self):
        Path(self.final).write_bytes(b"v")
        rec = self._rec(video_path=self.final)
        self.assertEqual(self.mod._finalize_recording(rec), self.final)

    def test_mux_sys_and_mic(self):
        Path(self.final[:-4] + "-sys.wav").write_bytes(b"s" * 100)
        Path(self.final[:-4] + "-mic.wav").write_bytes(b"m" * 100)
        runs = []

        def fake_run(cmd, **k):
            runs.append(cmd)
            Path(self.final).write_bytes(b"muxed")
            return mock.Mock(returncode=0)

        with mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            out = self.mod._finalize_recording(self._rec())
        self.assertEqual(out, self.final)
        self.assertEqual(len(runs), 1)
        self.assertIn("amix=inputs=2", " ".join(runs[0]))
        self.assertFalse(os.path.exists(self.video))          # raw video removed
        self.assertFalse(os.path.exists(self.final[:-4] + "-sys.wav"))  # wavs cleaned

    def test_mux_sys_only_no_amix(self):
        Path(self.final[:-4] + "-sys.wav").write_bytes(b"s" * 100)
        runs = []

        def fake_run(cmd, **k):
            runs.append(cmd)
            Path(self.final).write_bytes(b"muxed")
            return mock.Mock(returncode=0)

        with mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            out = self.mod._finalize_recording(self._rec())
        self.assertEqual(out, self.final)
        self.assertNotIn("amix", " ".join(runs[0]))

    def test_no_audio_renames_video(self):
        # no wav files at all → silent video shipped under the final path
        out = self.mod._finalize_recording(self._rec())
        self.assertEqual(out, self.final)
        self.assertTrue(os.path.exists(self.final))
        self.assertFalse(os.path.exists(self.video))

    def test_mux_failure_falls_back_to_video(self):
        Path(self.final[:-4] + "-sys.wav").write_bytes(b"s" * 100)
        with mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "run",
                               side_effect=RuntimeError("boom")):
            out = self.mod._finalize_recording(self._rec())
        self.assertEqual(out, self.final)
        self.assertTrue(os.path.exists(self.final))  # raw video preserved as final

    def test_send_signal_failure_does_not_raise(self):
        # a proc that's already dead (send_signal raises ProcessLookupError)
        # must not abort finalization — cleanup continues past it.
        rec = self._rec(proc=RaisingProc(["screencapture", self.video]))
        out = self.mod._finalize_recording(rec)
        self.assertEqual(out, self.final)

    def test_wav_cleanup_failure_does_not_raise(self):
        Path(self.final[:-4] + "-sys.wav").write_bytes(b"s" * 100)
        with mock.patch.object(self.mod, "_ffmpeg", return_value="/ff/ffmpeg"), \
             mock.patch.object(self.mod.subprocess, "run",
                               side_effect=lambda cmd, **k: Path(self.final).write_bytes(b"m")), \
             mock.patch.object(self.mod.os, "unlink", side_effect=OSError("busy")):
            out = self.mod._finalize_recording(self._rec())
        self.assertEqual(out, self.final)


class TestHttpAudioModes(unittest.TestCase):
    """Drive /capture-video through the real handler with mocks."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        os.environ["SUTANDO_SCREENSHOT_DIR"] = cls.tmpdir
        cls.mod = load_module()
        cls.mod.NOTIFY_ENABLED = False
        import http.server as hs
        cls.httpd = hs.HTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        os.environ.pop("SUTANDO_SCREENSHOT_DIR", None)

    def _get(self, q):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/capture-video?{q}",
            headers={"X-Sutando-Capture-Token": self.mod.CAPTURE_TOKEN or ""})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _reset(self):
        with self.mod._recording_lock:
            self.mod._active_recording = None

    def test_mix_registers_tap_and_video_suffix(self):
        self._reset()
        tap = FakeProc(["tap", "x-sys.wav"])
        with mock.patch.object(self.mod, "_spawn_audio_captures",
                               return_value=(tap, None, False)), \
             mock.patch.object(self.mod.subprocess, "Popen", FakeProc), \
             mock.patch.object(self.mod.threading, "Timer") as t:
            t.return_value.daemon = True
            body = self._get("action=start&silent=true&audio=on")  # `on` → mix
            self.assertEqual(body["status"], "recording")
            rec = self.mod._active_recording
            self.assertIs(rec["tap"], tap)
            self.assertTrue(rec["video_path"].endswith("-video.mov"))
            self.assertNotEqual(rec["video_path"], rec["path"])
            # screencapture must NOT carry -g in tap mode
            sc = [c for c in [rec["proc"].cmd] if c[0] == "screencapture"][0]
            self.assertNotIn("-g", sc)
            # stop returns the FINAL path via _finalize_recording
            with mock.patch.object(self.mod, "_finalize_recording",
                                   return_value=rec["path"]) as fin:
                out = self._get("action=stop&silent=true")
                self.assertEqual(out["status"], "ok")
                self.assertEqual(out["path"], rec["path"])
                fin.assert_called_once()

    def test_legacy_mic_keeps_dash_g(self):
        self._reset()
        with mock.patch.object(self.mod.subprocess, "Popen", FakeProc), \
             mock.patch.object(self.mod.threading, "Timer") as t:
            t.return_value.daemon = True
            body = self._get("action=start&silent=true&audio=mic")
            rec = self.mod._active_recording
            self.assertIn("-g", rec["proc"].cmd)
            self.assertEqual(rec["video_path"], rec["path"])  # no mux step
            self.assertIsNone(rec["tap"])
        self._get("action=stop&silent=true")

    def test_tap_fallback_lands_on_dash_g(self):
        self._reset()
        with mock.patch.object(self.mod, "_spawn_audio_captures",
                               return_value=(None, None, True)), \
             mock.patch.object(self.mod.subprocess, "Popen", FakeProc), \
             mock.patch.object(self.mod.threading, "Timer") as t:
            t.return_value.daemon = True
            self._get("action=start&silent=true&audio=mix")
            rec = self.mod._active_recording
            self.assertIn("-g", rec["proc"].cmd)
            self.assertEqual(rec["video_path"], rec["path"])
        self._get("action=stop&silent=true")

    def test_screencapture_spawn_failure_kills_tap_and_mic(self):
        self._reset()
        tap, mic = FakeProc(["tap"]), FakeProc(["mic"])
        killed = []
        tap.kill = lambda: killed.append("tap")
        mic.kill = lambda: killed.append("mic")
        with mock.patch.object(self.mod, "_spawn_audio_captures",
                               return_value=(tap, mic, False)), \
             mock.patch.object(self.mod.subprocess, "Popen",
                               side_effect=OSError("screencapture missing")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("action=start&silent=true&audio=mix")
        self.assertEqual(ctx.exception.code, 500)
        self.assertCountEqual(killed, ["tap", "mic"])
        ctx.exception.close()

    def test_screencapture_spawn_failure_survives_tap_kill_raising(self):
        # kill() itself raising must not mask the original 500 response.
        self._reset()
        tap = FakeProc(["tap"])
        tap.kill = lambda: (_ for _ in ()).throw(OSError("already dead"))
        with mock.patch.object(self.mod, "_spawn_audio_captures",
                               return_value=(tap, None, False)), \
             mock.patch.object(self.mod.subprocess, "Popen",
                               side_effect=OSError("screencapture missing")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._get("action=start&silent=true&audio=mix")
        self.assertEqual(ctx.exception.code, 500)
        ctx.exception.close()

    def test_watchdog_auto_stop_survives_finalize_raising(self):
        self._reset()
        with mock.patch.object(self.mod.subprocess, "Popen", FakeProc), \
             mock.patch.object(self.mod.threading, "Timer") as t:
            t.return_value.daemon = True
            self._get("action=start&silent=true&audio=mic")
            auto_stop = t.call_args[0][1]
        with mock.patch.object(self.mod, "_finalize_recording",
                               side_effect=RuntimeError("mux blew up")):
            auto_stop()  # must not raise despite _finalize_recording failing
        self.assertIsNone(self.mod._active_recording)


if __name__ == "__main__":
    unittest.main(verbosity=2)

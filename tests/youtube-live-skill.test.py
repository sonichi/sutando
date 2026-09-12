#!/usr/bin/env python3
"""Unit tests for the youtube-live skill. CI-safe: no network, no ffmpeg spawn."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "skills" / "youtube-live" / "scripts" / "go_live.py"
)
_spec = importlib.util.spec_from_file_location("go_live", _SCRIPT)
go_live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(go_live)

CFG = {"resolution": "1280x720", "fps": 30, "video_bitrate": "4500k",
       "audio_bitrate": "128k", "buffer_size": "9000k", "avf_screen_spec": "1:0"}


class BuildCmdTests(unittest.TestCase):
    def test_test_source_has_lavfi_video_and_audio(self):
        cmd = go_live.build_ffmpeg_cmd("test", "KEY123", CFG)
        joined = " ".join(cmd)
        self.assertIn("testsrc2=size=1280x720:rate=30", joined)
        self.assertIn("sine=frequency=1000", joined)
        # YouTube-compatible codecs
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("yuv420p", cmd)
        # gop = 2 * fps
        gi = cmd.index("-g")
        self.assertEqual(cmd[gi + 1], "60")

    def test_ends_with_flv_to_ingest_and_key(self):
        cmd = go_live.build_ffmpeg_cmd("test", "SECRET", CFG,
                                       ingest_base="rtmp://x/live2")
        self.assertEqual(cmd[-3:], ["-f", "flv", "rtmp://x/live2/SECRET"])

    def test_file_source_loop(self):
        cmd = go_live.build_ffmpeg_cmd("file:/tmp/a.mp4", "K", CFG, loop=True)
        self.assertIn("-stream_loop", cmd)
        self.assertIn("/tmp/a.mp4", cmd)

    def test_file_source_no_loop(self):
        cmd = go_live.build_ffmpeg_cmd("file:/tmp/a.mp4", "K", CFG, loop=False)
        self.assertNotIn("-stream_loop", cmd)

    def test_image_source_has_still_and_silent_audio(self):
        cmd = go_live.build_ffmpeg_cmd("image:/tmp/s.png", "K", CFG)
        joined = " ".join(cmd)
        self.assertIn("-loop", cmd)
        self.assertIn("/tmp/s.png", cmd)
        self.assertIn("anullsrc", joined)

    def test_screen_source_uses_avfoundation(self):
        cmd = go_live.build_ffmpeg_cmd("screen", "K", CFG)
        self.assertIn("avfoundation", cmd)
        self.assertIn("1:0", cmd)

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            go_live.build_ffmpeg_cmd("bogus", "K", CFG)


class RedactionTests(unittest.TestCase):
    def test_key_is_redacted_in_printed_command(self):
        cmd = go_live.build_ffmpeg_cmd("test", "SUPERSECRETKEY", CFG)
        printed = go_live._redacted_str(cmd, "SUPERSECRETKEY")
        self.assertNotIn("SUPERSECRETKEY", printed)
        self.assertIn("<STREAM_KEY>", printed)


class KeyResolutionTests(unittest.TestCase):
    def test_cli_beats_env(self):
        os.environ["YOUTUBE_STREAM_KEY"] = "envkey"
        try:
            self.assertEqual(go_live._resolve_stream_key("clikey"), "clikey")
        finally:
            del os.environ["YOUTUBE_STREAM_KEY"]

    def test_env_used_when_no_cli(self):
        os.environ["YOUTUBE_STREAM_KEY"] = "envkey"
        try:
            self.assertEqual(go_live._resolve_stream_key(None), "envkey")
        finally:
            del os.environ["YOUTUBE_STREAM_KEY"]


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_stream_and_redacts(self):
        import io
        import json
        from contextlib import redirect_stdout

        class Args:
            source = "test"
            loop = False
            stream_key = "TOPSECRET"
            dry_run = True

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = go_live.cmd_start(Args())
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["dry_run"])
        self.assertNotIn("TOPSECRET", out["command"])
        self.assertIn("<STREAM_KEY>", out["command"])


class StatusTests(unittest.TestCase):
    def test_status_not_running(self):
        import io
        import json
        from contextlib import redirect_stdout

        # Ensure no stale pid file interferes.
        if os.path.exists(go_live.PID_FILE):
            self.skipTest("a real stream pid file exists; skip to avoid touching it")
        buf = io.StringIO()
        with redirect_stdout(buf):
            go_live.cmd_status(None)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["running"])


class _Args:
    def __init__(self, **kw):
        self.source = kw.get("source", "test")
        self.loop = kw.get("loop", False)
        self.stream_key = kw.get("stream_key", None)
        self.dry_run = kw.get("dry_run", False)
        self.startup_grace = kw.get("startup_grace", 0.0)


def _capture(fn, *a):
    import io
    import json
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*a)
    return rc, json.loads(buf.getvalue())


class CmdStartBranchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._orig_pid_file = go_live.PID_FILE
        self._orig_log = go_live.FFMPEG_LOG
        go_live.PID_FILE = os.path.join(self._tmp, "pid")
        go_live.FFMPEG_LOG = os.path.join(self._tmp, "ffmpeg.log")

    def tearDown(self):
        go_live.PID_FILE = self._orig_pid_file
        go_live.FFMPEG_LOG = self._orig_log

    def test_start_refuses_when_already_running(self):
        from unittest import mock
        with mock.patch.object(go_live, "_running_pid", return_value=4321):
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("already running", out["error"])

    def test_start_errors_when_ffmpeg_missing(self):
        # ffmpeg is only checked for a real stream (after key + build + dry-run),
        # so provide a key and a non-dry-run Args to reach that gate.
        from unittest import mock
        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value="K"), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value=None):
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 1)
        self.assertIn("ffmpeg", out["error"])

    def test_start_errors_when_no_key(self):
        from unittest import mock
        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value="/x/ffmpeg"), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value=None):
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 1)
        self.assertIn("stream key", out["error"])

    def test_start_errors_on_unknown_source(self):
        from unittest import mock
        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value="/x/ffmpeg"), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value="K"):
            rc, out = _capture(go_live.cmd_start, _Args(source="bogus"))
        self.assertEqual(rc, 1)
        self.assertIn("unknown source", out["error"])

    def test_start_spawns_and_writes_pidfile(self):
        from unittest import mock

        class FakeProc:
            pid = 9999

            def poll(self):
                return None  # still alive after the grace window

        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value="/x/ffmpeg"), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value="K"), \
             mock.patch.object(go_live.subprocess, "Popen", return_value=FakeProc()) as popen:
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 0)
        self.assertTrue(out["started"])
        self.assertEqual(out["pid"], 9999)
        popen.assert_called_once()
        self.assertEqual(Path(go_live.PID_FILE).read_text().strip(), "9999")
        # pid file is owner-only (0600)
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(go_live.PID_FILE).st_mode), 0o600)

    def test_start_fails_if_ffmpeg_exits_immediately(self):
        from unittest import mock

        class DeadProc:
            pid = 4242
            returncode = 1

            def poll(self):
                return 1  # already exited

        def fake_popen(*a, **k):
            # ffmpeg emits an error to its stderr log, then dies immediately.
            Path(go_live.FFMPEG_LOG).write_text("Connection refused to rtmp ingest")
            return DeadProc()

        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value="/x/ffmpeg"), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value="K"), \
             mock.patch.object(go_live.subprocess, "Popen", side_effect=fake_popen):
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("exited immediately", out["error"])
        self.assertIn("Connection refused", out["ffmpeg_stderr"])
        self.assertFalse(os.path.exists(go_live.PID_FILE))  # no pid written for a dead stream

    def test_start_failure_redacts_stream_key_from_ffmpeg_stderr(self):
        # bassil CR 2026-07-31: ffmpeg echoes the full rtmp target (key
        # included) into stderr on connect failures, and the ffmpeg_stderr
        # diagnostics path surfaced it raw — the key-is-never-printed
        # contract must hold on the failure path, not just --dry-run.
        import json as _json
        from unittest import mock

        class DeadProc:
            pid = 4242
            returncode = 1

            def poll(self):
                return 1

        secret = "sk-live-STREAMKEY-4242"

        def fake_popen(*a, **k):
            Path(go_live.FFMPEG_LOG).write_text(
                f"[tcp] rtmp://a.rtmp.youtube.com/live2/{secret}: Connection refused\n"
                + "x" * 900  # push the first mention past the 800-char tail slice
                + f" retry rtmp://a.rtmp.youtube.com/live2/{secret} failed")
            return DeadProc()

        with mock.patch.object(go_live, "_running_pid", return_value=None), \
             mock.patch.object(go_live, "_ffmpeg_bin", return_value="/x/ffmpeg"), \
             mock.patch.object(go_live, "_resolve_stream_key", return_value=secret), \
             mock.patch.object(go_live.subprocess, "Popen", side_effect=fake_popen):
            rc, out = _capture(go_live.cmd_start, _Args())
        self.assertEqual(rc, 1)
        self.assertNotIn(secret, _json.dumps(out))  # nowhere in the emitted JSON
        self.assertIn(go_live._REDACTION, out["ffmpeg_stderr"])  # scrubbed, not dropped


class CmdStopStatusTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp()
        self._orig = go_live.PID_FILE
        go_live.PID_FILE = os.path.join(self._tmp, "pid")

    def tearDown(self):
        go_live.PID_FILE = self._orig

    def test_stop_when_nothing_running(self):
        rc, out = _capture(go_live.cmd_stop, None)
        self.assertEqual(rc, 0)
        self.assertFalse(out["stopped"])

    def test_stop_kills_running_pid(self):
        from unittest import mock
        Path(go_live.PID_FILE).write_text("5555")
        with mock.patch.object(go_live, "_running_pid", return_value=5555), \
             mock.patch.object(go_live, "_proc_looks_like_our_stream", return_value=True), \
             mock.patch.object(go_live.os, "kill") as kill:
            rc, out = _capture(go_live.cmd_stop, None)
        self.assertEqual(rc, 0)
        self.assertTrue(out["stopped"])
        kill.assert_called_once()
        self.assertFalse(os.path.exists(go_live.PID_FILE))

    def test_stop_refuses_foreign_process(self):
        # Recorded pid is live but ISN'T our ffmpeg (stale/reused) → never kill it.
        from unittest import mock
        Path(go_live.PID_FILE).write_text("5555")
        with mock.patch.object(go_live, "_running_pid", return_value=5555), \
             mock.patch.object(go_live, "_proc_looks_like_our_stream", return_value=False), \
             mock.patch.object(go_live.os, "kill") as kill:
            rc, out = _capture(go_live.cmd_stop, None)
        self.assertEqual(rc, 1)
        self.assertFalse(out["stopped"])
        kill.assert_not_called()  # crucial: we did NOT signal the foreign pid
        self.assertFalse(os.path.exists(go_live.PID_FILE))  # cleared the stale file

    def test_proc_looks_like_our_stream(self):
        from unittest import mock

        class R:
            stdout = "/x/ffmpeg -i rtmp://a.rtmp.youtube.com/live2/KEY"

        class R2:
            stdout = "/usr/bin/vim notes.txt"

        with mock.patch.object(go_live.subprocess, "run", return_value=R()):
            self.assertTrue(go_live._proc_looks_like_our_stream(123))
        with mock.patch.object(go_live.subprocess, "run", return_value=R2()):
            self.assertFalse(go_live._proc_looks_like_our_stream(123))

    def test_status_running(self):
        from unittest import mock
        with mock.patch.object(go_live, "_running_pid", return_value=7777):
            rc, out = _capture(go_live.cmd_status, None)
        self.assertTrue(out["running"])
        self.assertEqual(out["pid"], 7777)

    def test_running_pid_detects_self(self):
        Path(go_live.PID_FILE).write_text(str(os.getpid()))
        self.assertEqual(go_live._running_pid(), os.getpid())

    def test_running_pid_none_for_dead(self):
        Path(go_live.PID_FILE).write_text("999999")  # almost certainly not a live pid
        self.assertIsNone(go_live._running_pid())


class MiscBranchTests(unittest.TestCase):
    def test_manifest_config_returns_empty_on_error(self):
        from unittest import mock
        with mock.patch.object(go_live.json, "loads", side_effect=ValueError):
            self.assertEqual(go_live._load_manifest_config(), {})

    def test_resolve_stream_key_vault_path_returns_none_when_unset(self):
        # No CLI, no env → falls through to the vault branch. In CI there's no
        # YOUTUBE_STREAM_KEY in any vault/keyring, so it must resolve to None
        # (the broad except swallows any backend error).
        os.environ.pop("YOUTUBE_STREAM_KEY", None)
        self.assertIsNone(go_live._resolve_stream_key(None))

    def test_running_pid_malformed_file(self):
        import tempfile
        orig = go_live.PID_FILE
        tmp = tempfile.mkdtemp()
        go_live.PID_FILE = os.path.join(tmp, "pid")
        try:
            Path(go_live.PID_FILE).write_text("not-an-int")
            self.assertIsNone(go_live._running_pid())
        finally:
            go_live.PID_FILE = orig

    def test_stop_removes_stale_pidfile_when_not_running(self):
        import tempfile
        from unittest import mock
        orig = go_live.PID_FILE
        tmp = tempfile.mkdtemp()
        go_live.PID_FILE = os.path.join(tmp, "pid")
        try:
            Path(go_live.PID_FILE).write_text("123")
            with mock.patch.object(go_live, "_running_pid", return_value=None):
                rc, out = _capture(go_live.cmd_stop, None)
            self.assertEqual(rc, 0)
            self.assertFalse(out["stopped"])
            self.assertFalse(os.path.exists(go_live.PID_FILE))
        finally:
            go_live.PID_FILE = orig

    def test_stop_reports_error_when_kill_fails(self):
        import tempfile
        from unittest import mock
        orig = go_live.PID_FILE
        tmp = tempfile.mkdtemp()
        go_live.PID_FILE = os.path.join(tmp, "pid")
        try:
            Path(go_live.PID_FILE).write_text("444")
            with mock.patch.object(go_live, "_running_pid", return_value=444), \
                 mock.patch.object(go_live, "_proc_looks_like_our_stream", return_value=True), \
                 mock.patch.object(go_live.os, "kill", side_effect=OSError("nope")):
                rc, out = _capture(go_live.cmd_stop, None)
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
        finally:
            go_live.PID_FILE = orig


class MainDispatchTests(unittest.TestCase):
    def test_main_status(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = go_live.main(["status"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

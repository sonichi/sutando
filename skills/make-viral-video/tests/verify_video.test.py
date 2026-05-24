#!/usr/bin/env python3
"""
Tests for skills/make-viral-video/scripts/verify_video.py

Coverage:
  verify() — file_not_found, zero_byte_file, ffprobe_failed,
              no_video_stream, video_codec_not_h264, wrong_dimensions,
              no_audio_stream, audio_codec_not_aac, duration_outside_tolerance,
              av_duration_mismatch, valid (all checks pass)
  main()   — exit 0 on valid, exit 1 on invalid, --expected-duration flag

Run: python3 skills/make-viral-video/tests/verify_video.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "verify_video",
    REPO / "skills" / "make-viral-video" / "scripts" / "verify_video.py",
)
vv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vv)

TMPDIR = Path(tempfile.mkdtemp())


def _write(name: str, content: bytes = b"fake video data") -> Path:
    p = TMPDIR / name
    p.write_bytes(content)
    return p


def _probe(
    video_codec="h264", width=1280, height=720,
    audio_codec="aac", video_dur=45.0, audio_dur=45.0,
    total_dur=45.0,
):
    """Build a fake ffprobe JSON payload."""
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": video_codec,
                "width": width,
                "height": height,
                "duration": str(video_dur),
            },
            {
                "codec_type": "audio",
                "codec_name": audio_codec,
                "duration": str(audio_dur),
            },
        ],
        "format": {
            "duration": str(total_dur),
            "size": "1000000",
        },
    }


def _verify(path, probe_data=None, expected_duration_s=None):
    """Call verify() with a mocked ffprobe."""
    if probe_data is None:
        probe_data = _probe()
    with patch.object(vv, "ffprobe", return_value=probe_data):
        return vv.verify(path, expected_duration_s=expected_duration_s)


# ── verify ────────────────────────────────────────────────────────────────────

class TestVerify(unittest.TestCase):
    def test_file_not_found(self):
        result = vv.verify(TMPDIR / "ghost.mp4")
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_not_found")

    def test_zero_byte_file(self):
        p = _write("empty.mp4", b"")
        result = vv.verify(p)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "zero_byte_file")

    def test_ffprobe_failed(self):
        p = _write("bad.mp4")
        err = subprocess.CalledProcessError(1, "ffprobe", stderr="no such codec")
        with patch.object(vv, "ffprobe", side_effect=err):
            result = vv.verify(p)
        self.assertFalse(result["valid"])
        self.assertIn("ffprobe_failed", result["reason"])

    def test_no_video_stream(self):
        p = _write("novid.mp4")
        probe = _probe()
        probe["streams"] = [s for s in probe["streams"] if s["codec_type"] != "video"]
        result = _verify(p, probe_data=probe)
        self.assertFalse(result["valid"])
        self.assertIn("no_video_stream", result["reason"])

    def test_video_codec_not_h264(self):
        p = _write("hevc.mp4")
        result = _verify(p, probe_data=_probe(video_codec="hevc"))
        self.assertFalse(result["valid"])
        self.assertIn("h264", result["reason"])

    def test_wrong_dimensions_portrait(self):
        p = _write("portrait.mp4")
        result = _verify(p, probe_data=_probe(width=1080, height=1920))
        self.assertFalse(result["valid"])
        self.assertIn("wrong_dimensions", result["reason"])

    def test_wrong_dimensions_hd_not_720p(self):
        p = _write("1080p.mp4")
        result = _verify(p, probe_data=_probe(width=1920, height=1080))
        self.assertFalse(result["valid"])
        self.assertIn("wrong_dimensions", result["reason"])

    def test_no_audio_stream(self):
        p = _write("silent.mp4")
        probe = _probe()
        probe["streams"] = [s for s in probe["streams"] if s["codec_type"] != "audio"]
        result = _verify(p, probe_data=probe)
        self.assertFalse(result["valid"])
        self.assertIn("no_audio_stream", result["reason"])

    def test_audio_codec_not_aac(self):
        p = _write("mp3audio.mp4")
        result = _verify(p, probe_data=_probe(audio_codec="mp3"))
        self.assertFalse(result["valid"])
        self.assertIn("aac", result["reason"])

    def test_duration_too_short(self):
        p = _write("short.mp4")
        probe = _probe(total_dur=10.0, video_dur=10.0, audio_dur=10.0)
        result = _verify(p, probe_data=probe, expected_duration_s=45.0)
        self.assertFalse(result["valid"])
        self.assertIn("duration_outside_tolerance", result["reason"])

    def test_duration_too_long(self):
        p = _write("long.mp4")
        probe = _probe(total_dur=90.0, video_dur=90.0, audio_dur=90.0)
        result = _verify(p, probe_data=probe, expected_duration_s=45.0)
        self.assertFalse(result["valid"])
        self.assertIn("duration_outside_tolerance", result["reason"])

    def test_duration_within_tolerance_ok(self):
        p = _write("within.mp4")
        probe = _probe(total_dur=47.0, video_dur=47.0, audio_dur=47.0)
        result = _verify(p, probe_data=probe, expected_duration_s=45.0)
        self.assertTrue(result["valid"])

    def test_av_duration_mismatch(self):
        # audio drops off 2 seconds early — Lucy's v11 regression
        p = _write("avmismatch.mp4")
        probe = _probe(video_dur=45.0, audio_dur=42.0, total_dur=45.0)
        result = _verify(p, probe_data=probe)
        self.assertFalse(result["valid"])
        self.assertIn("av_duration_mismatch", result["reason"])

    def test_valid_all_checks_pass(self):
        p = _write("good.mp4")
        result = _verify(p)
        self.assertTrue(result["valid"])

    def test_valid_has_report_fields(self):
        p = _write("report.mp4")
        result = _verify(p)
        self.assertIn("duration_s", result)
        self.assertIn("size_bytes", result)
        self.assertIn("streams", result)

    def test_no_expected_duration_skips_check(self):
        p = _write("nodur.mp4")
        probe = _probe(total_dur=999.0, video_dur=999.0, audio_dur=999.0)
        result = _verify(p, probe_data=probe, expected_duration_s=None)
        self.assertTrue(result["valid"])


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def _run(self, path, extra_args=None):
        argv = [str(path)] + (extra_args or [])
        with patch("sys.stdout", new_callable=StringIO) as out, \
             patch.object(vv, "ffprobe", return_value=_probe()):
            try:
                code = vv.main()
            except SystemExit as e:
                code = e.code
            return code, out.getvalue()

    def test_valid_file_exits_0(self):
        p = _write("main_valid.mp4")
        # Patch sys.argv and ffprobe
        with patch("sys.argv", ["verify_video.py", str(p)]), \
             patch.object(vv, "ffprobe", return_value=_probe()), \
             patch("sys.stdout", new_callable=StringIO) as out:
            try:
                code = vv.main()
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result["valid"])

    def test_missing_file_exits_1(self):
        with patch("sys.argv", ["verify_video.py", str(TMPDIR / "ghost.mp4")]), \
             patch("sys.stdout", new_callable=StringIO) as out:
            try:
                code = vv.main()
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result["valid"])

    def test_expected_duration_flag(self):
        p = _write("dur_flag.mp4")
        probe = _probe(total_dur=90.0, video_dur=90.0, audio_dur=90.0)
        with patch("sys.argv", ["verify_video.py", str(p), "--expected-duration", "45"]), \
             patch.object(vv, "ffprobe", return_value=probe), \
             patch("sys.stdout", new_callable=StringIO) as out:
            try:
                code = vv.main()
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertIn("duration_outside_tolerance", result["reason"])


if __name__ == "__main__":
    unittest.main()

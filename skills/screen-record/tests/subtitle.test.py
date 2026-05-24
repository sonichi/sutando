#!/usr/bin/env python3
"""
Tests for skills/screen-record/scripts/subtitle.py

Coverage:
  find_latest_recording — found path, file missing, empty stdout
  transcribe            — success (SRT created by whisper mock), no SRT produced,
                          cleanup of wav even on success
  burn_subtitles        — output file created, output missing returns None
  main                  — no recording, transcription failure, burn failure, success

Run: python3 skills/screen-record/tests/subtitle.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "subtitle",
    REPO / "skills" / "screen-record" / "scripts" / "subtitle.py",
)
sub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sub)

TMPDIR = Path(tempfile.mkdtemp())
_FAKE_SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n"


def _fake_run(returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = ""
    m.stderr = ""
    return m


# ── find_latest_recording ─────────────────────────────────────────────────────

class TestFindLatestRecording(unittest.TestCase):
    def test_returns_path_when_file_exists(self):
        p = TMPDIR / "sutando-recording-001.mov"
        p.write_bytes(b"fake")
        with patch("subprocess.run", return_value=MagicMock(stdout=str(p), returncode=0)):
            result = sub.find_latest_recording()
        self.assertEqual(result, str(p))

    def test_returns_none_when_stdout_empty(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)):
            result = sub.find_latest_recording()
        self.assertIsNone(result)

    def test_returns_none_when_file_not_on_disk(self):
        with patch("subprocess.run", return_value=MagicMock(stdout="/tmp/nonexistent.mov", returncode=0)):
            result = sub.find_latest_recording()
        self.assertIsNone(result)


# ── transcribe ────────────────────────────────────────────────────────────────

class TestTranscribe(unittest.TestCase):
    def _whisper_side_effect(self, srt_dir_holder):
        """subprocess.run mock that writes a fake SRT when whisper is called."""
        def _effect(cmd, **kwargs):
            if cmd[0] == "whisper":
                # Extract --output_dir value
                idx = cmd.index("--output_dir")
                srt_dir = cmd[idx + 1]
                srt_dir_holder.append(srt_dir)
                wav_arg = cmd[1]
                srt_name = os.path.basename(wav_arg).replace(".wav", ".srt")
                with open(os.path.join(srt_dir, srt_name), "w") as f:
                    f.write(_FAKE_SRT)
            return _fake_run()
        return _effect

    def test_success_returns_srt_content(self):
        video = TMPDIR / "test.mov"
        video.write_bytes(b"fake")
        holder = []
        with patch("subprocess.run", side_effect=self._whisper_side_effect(holder)):
            content, srt_path = sub.transcribe(str(video))
        self.assertEqual(content, _FAKE_SRT)
        self.assertIsNotNone(srt_path)

    def test_no_srt_returns_none(self):
        video = TMPDIR / "nosrt.mov"
        video.write_bytes(b"fake")
        # Both subprocess calls succeed but write no SRT file
        with patch("subprocess.run", return_value=_fake_run()):
            content, srt_path = sub.transcribe(str(video))
        self.assertIsNone(content)
        self.assertIsNone(srt_path)

    def test_wav_cleaned_up_on_success(self):
        video = TMPDIR / "cleanup.mov"
        video.write_bytes(b"fake")
        holder = []
        created_wav = []

        original_ntf = tempfile.NamedTemporaryFile

        def _track_ntf(**kwargs):
            f = original_ntf(**kwargs)
            created_wav.append(f.name)
            return f

        with patch("tempfile.NamedTemporaryFile", side_effect=_track_ntf), \
             patch("subprocess.run", side_effect=self._whisper_side_effect(holder)):
            sub.transcribe(str(video))

        # The wav file should have been deleted
        if created_wav:
            self.assertFalse(os.path.exists(created_wav[0]))


# ── burn_subtitles ────────────────────────────────────────────────────────────

class TestBurnSubtitles(unittest.TestCase):
    def test_success_returns_output_path_and_size(self):
        video = str(TMPDIR / "input.mov")
        srt = str(TMPDIR / "input.srt")
        out = video.replace(".mov", "-subtitled.mov")

        def _run_side_effect(cmd, **kwargs):
            # Create the output file so burn_subtitles sees it (>1MB so size rounds > 0)
            (TMPDIR / "input-subtitled.mov").write_bytes(b"x" * (2 * 1024 * 1024))
            return _fake_run()

        with patch("subprocess.run", side_effect=_run_side_effect):
            out_path, size_mb = sub.burn_subtitles(video, srt)
        self.assertIsNotNone(out_path)
        self.assertGreater(size_mb, 0)

    def test_missing_output_returns_none(self):
        video = str(TMPDIR / "noout.mov")
        srt = str(TMPDIR / "noout.srt")
        with patch("subprocess.run", return_value=_fake_run()):
            out_path, size_mb = sub.burn_subtitles(video, srt)
        self.assertIsNone(out_path)
        self.assertEqual(size_mb, 0)

    def test_output_path_uses_subtitled_suffix(self):
        video = str(TMPDIR / "mysrc.mov")
        srt = str(TMPDIR / "mysrc.srt")
        out = str(TMPDIR / "mysrc-subtitled.mov")

        def _create_output(cmd, **kwargs):
            Path(out).write_bytes(b"y" * 512)
            return _fake_run()

        with patch("subprocess.run", side_effect=_create_output):
            out_path, _ = sub.burn_subtitles(video, srt)
        self.assertIn("subtitled", out_path)


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def _capture_stdout(self, fn):
        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            fn()
        return buf.getvalue()

    def test_no_recording_prints_error(self):
        with patch.object(sub, "find_latest_recording", return_value=None), \
             patch("sys.argv", ["subtitle.py"]):
            out = self._capture_stdout(sub.main)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_transcription_failure_prints_error(self):
        video = str(TMPDIR / "src.mov")
        Path(video).write_bytes(b"fake")
        with patch.object(sub, "find_latest_recording", return_value=video), \
             patch.object(sub, "transcribe", return_value=(None, None)), \
             patch("sys.argv", ["subtitle.py"]):
            out = self._capture_stdout(sub.main)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_burn_failure_prints_error(self):
        video = str(TMPDIR / "src2.mov")
        srt = str(TMPDIR / "src2.srt")
        Path(video).write_bytes(b"fake")
        with patch.object(sub, "find_latest_recording", return_value=video), \
             patch.object(sub, "transcribe", return_value=(_FAKE_SRT, srt)), \
             patch.object(sub, "burn_subtitles", return_value=(None, 0)), \
             patch("sys.argv", ["subtitle.py"]):
            out = self._capture_stdout(sub.main)
        data = json.loads(out)
        self.assertIn("error", data)

    def test_success_prints_done(self):
        video = str(TMPDIR / "src3.mov")
        srt = str(TMPDIR / "src3.srt")
        out_path = str(TMPDIR / "src3-subtitled.mov")
        Path(video).write_bytes(b"fake")
        with patch.object(sub, "find_latest_recording", return_value=video), \
             patch.object(sub, "transcribe", return_value=(_FAKE_SRT, srt)), \
             patch.object(sub, "burn_subtitles", return_value=(out_path, 2.5)), \
             patch("sys.argv", ["subtitle.py"]):
            out = self._capture_stdout(sub.main)
        data = json.loads(out)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["path"], out_path)

    def test_explicit_video_arg_skips_find(self):
        video = str(TMPDIR / "explicit.mov")
        Path(video).write_bytes(b"fake")
        with patch.object(sub, "find_latest_recording") as mock_find, \
             patch.object(sub, "transcribe", return_value=(None, None)), \
             patch("sys.argv", ["subtitle.py", video]):
            self._capture_stdout(sub.main)
        mock_find.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Tests for skills/screen-record/scripts/record.py

Coverage:
  _list_audio_devices — parses avfoundation stderr: multi-device, audio-only
                        (skips video header), no-devices section, ffmpeg exception
  _has_audio_device   — True with devices, False when empty
  _pick_audio_device  — macbook mic wins, non-virtual fallback, all-virtual fallback,
                        no devices returns None

Run: python3 skills/screen-record/tests/record.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "record",
    REPO / "skills" / "screen-record" / "scripts" / "record.py",
)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)


def _fake_ffmpeg_stderr(audio_devices):
    """Build fake avfoundation -list_devices stderr with the given audio devices."""
    lines = [
        "[AVFoundation indev @ 0xdeadbeef] AVFoundation video devices:",
        "[AVFoundation indev @ 0xdeadbeef] [0] FaceTime HD Camera",
        "[AVFoundation indev @ 0xdeadbeef] AVFoundation audio devices:",
    ]
    for idx, name in audio_devices:
        lines.append(f"[AVFoundation indev @ 0xdeadbeef] [{idx}] {name}")
    lines.append("Error: audio=default:video=default")
    return "\n".join(lines)


def _mock_ffmpeg(stderr):
    m = MagicMock()
    m.stderr = stderr
    m.returncode = 0
    return patch("subprocess.run", return_value=m)


# ── _list_audio_devices ───────────────────────────────────────────────────────

class TestListAudioDevices(unittest.TestCase):
    def test_single_device_parsed(self):
        stderr = _fake_ffmpeg_stderr([(0, "MacBook Pro Microphone")])
        with _mock_ffmpeg(stderr):
            result = rec._list_audio_devices()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (0, "MacBook Pro Microphone"))

    def test_multi_device_all_parsed(self):
        stderr = _fake_ffmpeg_stderr([
            (0, "MacBook Pro Microphone"),
            (1, "ZoomAudioDevice"),
            (2, "BlackHole 2ch"),
        ])
        with _mock_ffmpeg(stderr):
            result = rec._list_audio_devices()
        self.assertEqual(len(result), 3)

    def test_no_audio_section_returns_empty(self):
        stderr = "[AVFoundation indev @ 0x0] AVFoundation video devices:\n[AVFoundation indev @ 0x0] [0] Camera"
        with _mock_ffmpeg(stderr):
            result = rec._list_audio_devices()
        self.assertEqual(result, [])

    def test_ffmpeg_exception_returns_empty(self):
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            result = rec._list_audio_devices()
        self.assertEqual(result, [])

    def test_index_and_name_correct(self):
        stderr = _fake_ffmpeg_stderr([(3, "USB Microphone")])
        with _mock_ffmpeg(stderr):
            result = rec._list_audio_devices()
        self.assertEqual(result[0][0], 3)
        self.assertEqual(result[0][1], "USB Microphone")

    def test_video_devices_not_included(self):
        stderr = _fake_ffmpeg_stderr([(0, "RealMic")])
        with _mock_ffmpeg(stderr):
            result = rec._list_audio_devices()
        # FaceTime HD Camera (video) must not appear
        names = [name for _, name in result]
        self.assertNotIn("FaceTime HD Camera", names)


# ── _has_audio_device ─────────────────────────────────────────────────────────

class TestHasAudioDevice(unittest.TestCase):
    def test_true_when_devices_present(self):
        with patch.object(rec, "_list_audio_devices", return_value=[(0, "Mic")]):
            self.assertTrue(rec._has_audio_device())

    def test_false_when_no_devices(self):
        with patch.object(rec, "_list_audio_devices", return_value=[]):
            self.assertFalse(rec._has_audio_device())


# ── _pick_audio_device ────────────────────────────────────────────────────────

class TestPickAudioDevice(unittest.TestCase):
    def test_macbook_mic_wins(self):
        devices = [
            (0, "ZoomAudioDevice"),
            (1, "MacBook Pro Microphone"),
            (2, "USB Mic"),
        ]
        with patch.object(rec, "_list_audio_devices", return_value=devices):
            result = rec._pick_audio_device()
        self.assertEqual(result, 1)

    def test_non_virtual_fallback_when_no_macbook(self):
        devices = [
            (0, "ZoomAudioDevice"),
            (1, "BlackHole 2ch"),
            (2, "USB Microphone"),
        ]
        with patch.object(rec, "_list_audio_devices", return_value=devices):
            result = rec._pick_audio_device()
        self.assertEqual(result, 2)

    def test_all_virtual_falls_back_to_first(self):
        devices = [
            (0, "ZoomAudioDevice"),
            (1, "BlackHole 2ch"),
            (2, "Microsoft Teams Audio"),
        ]
        with patch.object(rec, "_list_audio_devices", return_value=devices):
            result = rec._pick_audio_device()
        self.assertEqual(result, 0)

    def test_no_devices_returns_none(self):
        with patch.object(rec, "_list_audio_devices", return_value=[]):
            result = rec._pick_audio_device()
        self.assertIsNone(result)

    def test_single_non_virtual_device(self):
        devices = [(5, "External Microphone")]
        with patch.object(rec, "_list_audio_devices", return_value=devices):
            result = rec._pick_audio_device()
        self.assertEqual(result, 5)

    def test_macbook_air_mic_also_matches(self):
        devices = [(0, "MacBook Air Microphone")]
        with patch.object(rec, "_list_audio_devices", return_value=devices):
            result = rec._pick_audio_device()
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()

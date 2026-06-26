#!/usr/bin/env python3
"""Tests for skills/audio-transcribe/scripts/transcribe.py and bridge integration.

Covers:
  1. Skill script: supported / unsupported MIME, missing key, API error, subprocess exit codes.
  2. Bridge helper: skill absent → None, skill success → transcript, skill failure → None.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).parent.parent
SKILL_SCRIPT = REPO / "skills" / "audio-transcribe" / "scripts" / "transcribe.py"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_skill() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("transcribe", SKILL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_bridge_helper(bridge_name: str) -> types.ModuleType:
    """Load only the _transcribe_via_skill function from a bridge file without
    executing the bridge's top-level connection setup."""
    bridge_path = REPO / "src" / f"{bridge_name}-bridge.py"
    src = bridge_path.read_text()
    # Extract just the helper function so we don't need the bridge's heavy deps.
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if "_transcribe_via_skill" in l and "def " in l)
    end = start + 1
    while end < len(lines) and (lines[end].startswith("    ") or lines[end] == ""):
        end += 1
    func_src = "\n".join(lines[start:end])
    # Inject __file__ so Path(__file__).parent.parent resolves correctly.
    ns: dict = {"Path": Path, "os": os, "sys": sys, "__file__": str(bridge_path)}
    exec(func_src, ns)  # noqa: S102
    mod = types.SimpleNamespace(_transcribe_via_skill=ns["_transcribe_via_skill"])
    return mod


# ---------------------------------------------------------------------------
# Skill script unit tests
# ---------------------------------------------------------------------------

class TestSkillMimeFilter(unittest.TestCase):
    def setUp(self):
        self.mod = _load_skill()

    def test_supported_extension_reaches_api(self):
        """An .m4a file with a valid key should attempt an API call."""
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
            f.write(b"\x00" * 16)
            path = f.name
        try:
            with patch.object(self.mod, "_api_key", return_value="fake-key"), \
                 patch("urllib.request.urlopen") as mock_url:
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = b'{"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}'
                mock_url.return_value = mock_resp
                result = self.mod.transcribe(path)
            self.assertEqual(result, "hello")
        finally:
            os.unlink(path)

    def test_unsupported_extension_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"data")
            path = f.name
        try:
            result = self.mod.transcribe(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_missing_api_key_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(b"data")
            path = f.name
        try:
            with patch.object(self.mod, "_api_key", return_value=""):
                result = self.mod.transcribe(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_api_error_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"data")
            path = f.name
        try:
            with patch.object(self.mod, "_api_key", return_value="fake-key"), \
                 patch("urllib.request.urlopen", side_effect=Exception("timeout")):
                result = self.mod.transcribe(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)


class TestSkillCLI(unittest.TestCase):
    def test_exit_1_on_unsupported_file(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"data")
            path = f.name
        try:
            r = subprocess.run(
                [sys.executable, str(SKILL_SCRIPT), path],
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stdout.strip(), "")
        finally:
            os.unlink(path)

    def test_exit_1_no_args(self):
        r = subprocess.run(
            [sys.executable, str(SKILL_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 1)


# ---------------------------------------------------------------------------
# Bridge helper tests
# ---------------------------------------------------------------------------

class TestBridgeHelperSlack(unittest.TestCase):
    def test_skill_absent_returns_none(self):
        mod = _load_bridge_helper("slack")
        with patch("pathlib.Path.exists", return_value=False):
            result = mod._transcribe_via_skill("/tmp/voice.m4a")
        self.assertIsNone(result)

    def test_skill_present_success(self):
        mod = _load_bridge_helper("slack")
        mock_result = MagicMock(returncode=0, stdout="hello world\n")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result):
            result = mod._transcribe_via_skill("/tmp/voice.m4a")
        self.assertEqual(result, "hello world")

    def test_skill_nonzero_exit_returns_none(self):
        mod = _load_bridge_helper("slack")
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result):
            result = mod._transcribe_via_skill("/tmp/voice.m4a")
        self.assertIsNone(result)

    def test_skill_subprocess_exception_returns_none(self):
        mod = _load_bridge_helper("slack")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", side_effect=Exception("timeout")):
            result = mod._transcribe_via_skill("/tmp/voice.m4a")
        self.assertIsNone(result)


class TestBridgeHelperDiscord(TestBridgeHelperSlack):
    def _load(self):
        return _load_bridge_helper("discord")

    def test_skill_absent_returns_none(self):
        mod = _load_bridge_helper("discord")
        with patch("pathlib.Path.exists", return_value=False):
            result = mod._transcribe_via_skill("/tmp/voice.ogg")
        self.assertIsNone(result)

    def test_skill_present_success(self):
        mod = _load_bridge_helper("discord")
        mock_result = MagicMock(returncode=0, stdout="discord transcript\n")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result):
            result = mod._transcribe_via_skill("/tmp/voice.ogg")
        self.assertEqual(result, "discord transcript")


class TestBridgeHelperTelegram(unittest.TestCase):
    def test_skill_absent_returns_none(self):
        mod = _load_bridge_helper("telegram")
        with patch("pathlib.Path.exists", return_value=False):
            result = mod._transcribe_via_skill("/tmp/voice.ogg")
        self.assertIsNone(result)

    def test_skill_present_success(self):
        mod = _load_bridge_helper("telegram")
        mock_result = MagicMock(returncode=0, stdout="telegram voice note text\n")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("subprocess.run", return_value=mock_result):
            result = mod._transcribe_via_skill("/tmp/voice.ogg")
        self.assertEqual(result, "telegram voice note text")


if __name__ == "__main__":
    unittest.main()

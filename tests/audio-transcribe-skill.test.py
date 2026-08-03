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
sys.path.insert(0, str(REPO / "src"))
from optional_script import run_optional_script  # noqa: E402

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
    ns: dict = {
        "Path": Path,
        "os": os,
        "sys": sys,
        "__file__": str(bridge_path),
        "_run_optional_script_shared": run_optional_script,
    }
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

class TestOptionalScriptRunner(unittest.TestCase):
    def test_success_preserves_interpreter_args_and_timeout(self):
        script = Path("/tmp/optional-script.py")
        result = MagicMock(returncode=0, stdout="  transcript text\n")
        with patch.object(Path, "exists", return_value=True), \
             patch("optional_script.subprocess.run", return_value=result) as run:
            output = run_optional_script(
                script, ["/tmp/voice.m4a"], timeout=25
            )
        self.assertEqual(output, "transcript text")
        run.assert_called_once_with(
            [sys.executable, str(script), "/tmp/voice.m4a"],
            capture_output=True,
            text=True,
            timeout=25,
        )

    def test_exception_is_fail_open_and_reported(self):
        errors = []
        with patch.object(Path, "exists", return_value=True), \
             patch("optional_script.subprocess.run", side_effect=TimeoutError("slow")):
            output = run_optional_script(
                Path("/tmp/optional-script.py"),
                ["input"],
                timeout=25,
                on_error=errors.append,
            )
        self.assertIsNone(output)
        self.assertEqual(str(errors[0]), "slow")

    def test_bridges_delegate_but_keep_capability_discovery_at_the_edge(self):
        shared_source = (REPO / "src" / "optional_script.py").read_text()
        self.assertNotIn("audio-transcribe", shared_source)
        self.assertNotIn(' / "skills"', shared_source)

        for bridge_name in ("discord", "slack", "telegram"):
            bridge_source = (
                REPO / "src" / f"{bridge_name}-bridge.py"
            ).read_text()
            lines = bridge_source.splitlines()
            start = next(
                i
                for i, line in enumerate(lines)
                if "def _transcribe_via_skill" in line
            )
            end = start + 1
            while end < len(lines) and (
                lines[end].startswith("    ") or lines[end] == ""
            ):
                end += 1
            helper_source = "\n".join(lines[start:end])
            self.assertIn("_run_optional_script_shared(", helper_source)
            self.assertNotIn("subprocess.run", helper_source)

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


class TestBridgeHelperSymlinkResolve(unittest.TestCase):
    """Regression guard for the symlink-resolve fix (a50d9c05, f6df7eb1).

    When a bridge is invoked via an app-bundle src/ symlink, Path(__file__)
    returns the symlink path, not the real file. Without resolving through the
    symlink, parent.parent points into the temp symlink dir — the skill is
    never found and the helper silently returns None. This test reproduces
    that scenario with a real symlink.

    Uses os.path.realpath(__file__) rather than Path(__file__).resolve() —
    the latter matches scripts/lint-workspace-resolution.sh's banned
    `Path(__file__).resolve().parent.parent` pattern (reserved for the
    workspace-root anti-pattern); this resolves a sibling skills/ *code*
    path, a different concern, so realpath() sidesteps the false-positive
    lint match while being equally symlink-safe.
    """

    def _build_symlink_ns(self, bridge_name: str, symlink_path: str) -> dict:
        """Extract the helper source and exec it with __file__ pointing at a symlink."""
        bridge_path = REPO / "src" / f"{bridge_name}-bridge.py"
        src = bridge_path.read_text()
        lines = src.splitlines()
        start = next(i for i, l in enumerate(lines) if "_transcribe_via_skill" in l and "def " in l)
        end = start + 1
        while end < len(lines) and (lines[end].startswith("    ") or lines[end] == ""):
            end += 1
        func_src = "\n".join(lines[start:end])
        ns: dict = {
            "Path": Path,
            "os": os,
            "sys": sys,
            "__file__": symlink_path,
            "_run_optional_script_shared": run_optional_script,
        }
        exec(func_src, ns)  # noqa: S102
        return ns

    def test_resolve_finds_skill_through_symlink(self):
        """With realpath resolution, the helper finds the skill even when invoked via symlink."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a symlink to slack-bridge.py inside a temp dir
            symlink = Path(tmpdir) / "slack-bridge.py"
            symlink.symlink_to(REPO / "src" / "slack-bridge.py")

            ns = self._build_symlink_ns("slack", str(symlink))
            helper = ns["_transcribe_via_skill"]

            # The skill script must be discovered at the real repo location.
            mock_result = MagicMock(returncode=0, stdout="resolved\n")
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                result = helper("/tmp/voice.m4a")

            # If realpath resolution works: subprocess.run is called (skill found) → "resolved"
            # If it's absent: Path(symlink).parent.parent != REPO → skill absent → None
            self.assertEqual(result, "resolved",
                "symlink resolution missing — helper returned None when invoked via symlink")
            self.assertTrue(mock_run.called, "subprocess.run never called — skill path not resolved")


class TestRealModuleResolveLineCoverage(unittest.TestCase):
    """Exercises the ACTUAL `_transcribe_via_skill` line in the real modules
    (not the extracted-and-exec'd copy `_load_bridge_helper`/
    `_build_symlink_ns` use above) — those helpers deliberately avoid
    importing the full bridge module (heavy top-level deps), which means
    coverage.py never sees the real file's `Path(os.path.realpath(__file__))`
    line execute. This class imports the real modules directly so that line
    gets real coverage credit, same call as the tests above."""

    def test_telegram_bridge_real_module(self):
        """telegram-bridge.py has no hard import-time dependency on a live
        token (see tests/telegram-writeside-attachments.test.py), so it can
        be imported directly."""
        with tempfile.TemporaryDirectory() as tmp_ws, patch.dict(os.environ, {
            "CLAUDE_CONFIG_DIR": str(Path(tmp_ws) / "claude"),
            "SUTANDO_WORKSPACE": tmp_ws,
            "SUTANDO_TEST_MODE": "1",
            "TELEGRAM_BOT_TOKEN": "test-token-not-real",
        }):
            spec = importlib.util.spec_from_file_location(
                "telegrambridge_realcov", REPO / "src" / "telegram-bridge.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            mock_result = MagicMock(returncode=0, stdout="resolved via real module\n")
            with patch("subprocess.run", return_value=mock_result):
                result = mod._transcribe_via_skill("/tmp/voice.ogg")
            self.assertEqual(result, "resolved via real module")

    def test_slack_bridge_real_module(self):
        """slack-bridge.py's module-level `App(token=BOT_TOKEN)` hits the
        real Slack API at import time and raises without a valid token —
        stub slack_bolt the same way tests/slack-bridge-chunking.test.py
        does so the import is hermetic."""

        class _RecordingClient:
            def chat_postMessage(self, **kwargs):
                return {"ok": True}

            def files_upload_v2(self, **kwargs):
                return {"ok": True}

        class _FakeApp:
            def __init__(self, token=None):
                self.client = _RecordingClient()

            def _decorator(self, *a, **k):
                return lambda fn: fn

            event = message = command = action = shortcut = view = _decorator

        _bolt = types.ModuleType("slack_bolt")
        _bolt.App = _FakeApp
        _adapter = types.ModuleType("slack_bolt.adapter")
        _socket = types.ModuleType("slack_bolt.adapter.socket_mode")
        _socket.SocketModeHandler = type(
            "SocketModeHandler", (), {"__init__": lambda self, *a, **k: None})
        with tempfile.TemporaryDirectory() as tmp_ws:
            claude_dir = Path(tmp_ws) / "claude"
            access_file = claude_dir / "channels" / "slack" / "access.json"
            access_file.parent.mkdir(parents=True)
            access_file.write_text('{"allowFrom": []}')
            with patch.dict(sys.modules, {
                "slack_bolt": _bolt,
                "slack_bolt.adapter": _adapter,
                "slack_bolt.adapter.socket_mode": _socket,
            }), patch.dict(os.environ, {
                "CLAUDE_CONFIG_DIR": str(claude_dir),
                "SUTANDO_WORKSPACE": tmp_ws,
                "SUTANDO_TEST_MODE": "1",
                "SLACK_BOT_TOKEN": "xoxb-test-not-real",
                "SLACK_APP_TOKEN": "xapp-test-not-real",
            }):
                spec = importlib.util.spec_from_file_location(
                    "slackbridge_realcov", REPO / "src" / "slack-bridge.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                mock_result = MagicMock(returncode=0, stdout="resolved via real module\n")
                with patch("subprocess.run", return_value=mock_result):
                    result = mod._transcribe_via_skill("/tmp/voice.m4a")
                self.assertEqual(result, "resolved via real module")


if __name__ == "__main__":
    unittest.main()

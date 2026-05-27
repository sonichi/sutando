#!/usr/bin/env python3
"""Tests for proactive-notify queue / voice / macos_notif action modules."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
ACTIONS_DIR = REPO / "skills" / "proactive-notify" / "scripts" / "actions"


def _load(name: str):
    path = ACTIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# queue action
# ---------------------------------------------------------------------------

class TestQueueModuleExists(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue((ACTIONS_DIR / "queue.py").exists())

    def test_send_callable(self):
        q = _load("queue")
        self.assertTrue(callable(q.send))


class TestQueueSend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ws = Path(self._tmp.name)
        # Patch resolve_workspace inside the already-loaded module.
        self._q = _load("queue")

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, body="hello"):
        fake_ping = MagicMock()
        fake_ping.name = "test-ping"
        fake_ping.urgency = "fyi"
        queue_file = self._ws / "skills" / "proactive-notify" / "state" / "queue.jsonl"
        with patch.object(self._q, "_QUEUE_FILE", queue_file):
            return self._q.send(fake_ping, body), queue_file

    def test_returns_ok(self):
        result, _ = self._send()
        self.assertTrue(result["ok"])

    def test_id_starts_with_q(self):
        result, _ = self._send()
        self.assertTrue(result["id"].startswith("q-"))

    def test_file_created(self):
        _, queue_file = self._send()
        self.assertTrue(queue_file.exists())

    def test_entry_is_valid_json(self):
        _, queue_file = self._send("some body text")
        line = queue_file.read_text().strip()
        record = json.loads(line)
        self.assertEqual(record["body"], "some body text")

    def test_record_has_required_fields(self):
        _, queue_file = self._send("msg")
        record = json.loads(queue_file.read_text().strip())
        for field in ("id", "ts", "ping", "urgency", "body"):
            self.assertIn(field, record)

    def test_record_ping_and_urgency(self):
        _, queue_file = self._send()
        record = json.loads(queue_file.read_text().strip())
        self.assertEqual(record["ping"], "test-ping")
        self.assertEqual(record["urgency"], "fyi")

    def test_second_send_appends(self):
        _, queue_file = self._send("first")
        with patch.object(self._q, "_QUEUE_FILE", queue_file):
            fake = MagicMock()
            fake.name = "p2"
            fake.urgency = "important"
            self._q.send(fake, "second")
        lines = [l for l in queue_file.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_oserror_returns_not_ok(self):
        # Point at a read-only directory so mkdir raises OSError.
        import os
        ro_dir = self._ws / "readonly_parent"
        ro_dir.mkdir(parents=True, exist_ok=True)
        ro_dir.chmod(0o444)
        queue_file = ro_dir / "subdir" / "queue.jsonl"
        try:
            fake = MagicMock()
            fake.name = "ping"
            fake.urgency = "fyi"
            with patch.object(self._q, "_QUEUE_FILE", queue_file):
                result = self._q.send(fake, "msg")
            self.assertFalse(result["ok"])
            self.assertIsInstance(result["error"], str)
        finally:
            ro_dir.chmod(0o755)

    def test_creates_parent_dirs(self):
        deep = self._ws / "a" / "b" / "c" / "queue.jsonl"
        with patch.object(self._q, "_QUEUE_FILE", deep):
            result = self._q.send(MagicMock(), "test")
        self.assertTrue(result["ok"])
        self.assertTrue(deep.exists())


# ---------------------------------------------------------------------------
# voice action
# ---------------------------------------------------------------------------

class TestVoiceModuleExists(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue((ACTIONS_DIR / "voice.py").exists())

    def test_send_callable(self):
        v = _load("voice")
        self.assertTrue(callable(v.send))


class TestVoiceSend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ws = Path(self._tmp.name)
        self._v = _load("voice")

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, body="speak this"):
        results_dir = self._ws / "results"
        with patch.object(self._v, "_RESULTS_DIR", results_dir):
            return self._v.send(MagicMock(), body), results_dir

    def test_returns_ok(self):
        result, _ = self._send()
        self.assertTrue(result["ok"])

    def test_id_is_timestamp_string(self):
        result, _ = self._send()
        self.assertIsInstance(result["id"], str)
        self.assertGreater(len(result["id"]), 0)

    def test_result_file_created(self):
        result, results_dir = self._send("hello voice")
        ts = result["id"]
        expected = results_dir / f"proactive-{ts}.txt"
        self.assertTrue(expected.exists())

    def test_result_file_contains_body(self):
        result, results_dir = self._send("say something")
        ts = result["id"]
        content = (results_dir / f"proactive-{ts}.txt").read_text()
        self.assertEqual(content, "say something")

    def test_filename_starts_with_proactive(self):
        _, results_dir = self._send()
        files = list(results_dir.glob("proactive-*.txt"))
        self.assertEqual(len(files), 1)

    def test_oserror_returns_not_ok(self):
        bad_dir = self._ws / "results"
        bad_dir.mkdir(parents=True, exist_ok=True)
        # Make the directory read-only so writing fails.
        bad_dir.chmod(0o444)
        try:
            with patch.object(self._v, "_RESULTS_DIR", bad_dir):
                result = self._v.send(MagicMock(), "fail")
            self.assertFalse(result["ok"])
        finally:
            bad_dir.chmod(0o755)

    def test_creates_results_dir_if_absent(self):
        results_dir = self._ws / "new_results"
        with patch.object(self._v, "_RESULTS_DIR", results_dir):
            result = self._v.send(MagicMock(), "body")
        self.assertTrue(result["ok"])
        self.assertTrue(results_dir.exists())


# ---------------------------------------------------------------------------
# macos_notif action
# ---------------------------------------------------------------------------

class TestMacosNotifModuleExists(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue((ACTIONS_DIR / "macos_notif.py").exists())

    def test_send_callable(self):
        m = _load("macos_notif")
        self.assertTrue(callable(m.send))

    def test_default_title_constant(self):
        m = _load("macos_notif")
        self.assertEqual(m._DEFAULT_TITLE, "Sutando")


class TestMacosNotifSend(unittest.TestCase):
    def setUp(self):
        self._m = _load("macos_notif")

    def _send(self, body="ping", env=None):
        import subprocess
        env = env or {}
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        with patch.dict("os.environ", env, clear=True):
            with patch("subprocess.run", mock_run):
                result = self._m.send(MagicMock(), body)
        return result, mock_run

    def test_returns_ok_on_success(self):
        result, _ = self._send()
        self.assertTrue(result["ok"])

    def test_id_is_notif(self):
        result, _ = self._send()
        self.assertEqual(result["id"], "notif")

    def test_calls_osascript(self):
        _, mock_run = self._send("hi")
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "osascript")
        self.assertEqual(call_args[1], "-e")

    def test_body_in_script(self):
        _, mock_run = self._send("meeting soon")
        script = mock_run.call_args[0][0][2]
        self.assertIn("meeting soon", script)

    def test_default_title_used(self):
        _, mock_run = self._send(env={})
        script = mock_run.call_args[0][0][2]
        self.assertIn("Sutando", script)

    def test_custom_title_from_env(self):
        _, mock_run = self._send(env={"SUTANDO_NOTIF_TITLE": "MyBot"})
        script = mock_run.call_args[0][0][2]
        self.assertIn("MyBot", script)

    def test_file_not_found_returns_not_ok(self):
        import subprocess
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = self._m.send(MagicMock(), "test")
        self.assertFalse(result["ok"])
        self.assertIn("osascript not found", result["error"])

    def test_timeout_returns_not_ok(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 5)):
            result = self._m.send(MagicMock(), "test")
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    def test_called_process_error_returns_not_ok(self):
        import subprocess
        err = subprocess.CalledProcessError(1, "osascript", stderr=b"permission denied")
        with patch("subprocess.run", side_effect=err):
            result = self._m.send(MagicMock(), "test")
        self.assertFalse(result["ok"])
        self.assertIn("permission denied", result["error"])

    def test_body_with_double_quotes_escaped(self):
        _, mock_run = self._send('say "hi"')
        script = mock_run.call_args[0][0][2]
        # Raw double quotes must not appear unescaped inside AppleScript string
        # (the body was 'say "hi"', should become 'say \\"hi\\"')
        self.assertIn('\\"hi\\"', script)

    def test_body_with_backslash_escaped(self):
        _, mock_run = self._send("path\\to\\file")
        script = mock_run.call_args[0][0][2]
        self.assertIn("path\\\\to\\\\file", script)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [
        TestQueueModuleExists, TestQueueSend,
        TestVoiceModuleExists, TestVoiceSend,
        TestMacosNotifModuleExists, TestMacosNotifSend,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

#!/usr/bin/env python3
"""Tests for _recover_orphan_sending_files() in src/slack-bridge.py (PR #1290).

Guards the startup-safety fix: if the bridge crashes between claiming a
proactive-*.txt (→ .sending) and delivering it, the next startup must
rename .sending back to .txt so the message is re-delivered.

Run: python3 tests/slack-bridge-orphan-recovery.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_bridge(workspace: Path):
    """Load slack-bridge.py with stubbed slack_bolt and a temp workspace."""
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)

    # Remove any cached module so env takes effect on fresh import.
    sys.modules.pop("slack_bridge_under_test", None)

    # Stub slack_bolt (may or may not be installed).
    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace()
        def event(self, _name):
            return lambda fn: fn

    try:
        import slack_bolt as _bolt
        _bolt.App = _StubApp
    except ImportError:
        stub = types.ModuleType("slack_bolt")
        stub.App = _StubApp
        sys.modules["slack_bolt"] = stub

    for pkg in ("slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            if pkg.endswith("socket_mode"):
                m.SocketModeHandler = object
            sys.modules[pkg] = m

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_under_test", REPO / "src" / "slack-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRecoverOrphanSendingFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slack-orphan-test-"))
        self.results = self.tmp / "results"
        self.results.mkdir(parents=True)
        self.mod = _load_bridge(self.tmp)
        # Redirect the module's RESULTS_DIR to our controlled temp dir.
        self.mod.RESULTS_DIR = self.results

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_results_returns_zero(self):
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 0)

    def test_missing_results_dir_returns_zero(self):
        shutil.rmtree(self.results)
        self.mod.RESULTS_DIR = self.results  # dir no longer exists
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 0)

    def test_single_orphan_renamed_to_txt(self):
        sending = self.results / "proactive-1234567890.sending"
        sending.write_text("hello world")
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 1)
        self.assertFalse(sending.exists(), ".sending file should be gone")
        recovered = self.results / "proactive-1234567890.txt"
        self.assertTrue(recovered.exists(), ".txt file should be created")
        self.assertEqual(recovered.read_text(), "hello world")

    def test_multiple_orphans_all_recovered(self):
        for i in range(3):
            (self.results / f"proactive-100000{i}.sending").write_text(f"msg{i}")
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 3)
        self.assertEqual(len(list(self.results.glob("*.txt"))), 3)
        self.assertEqual(len(list(self.results.glob("*.sending"))), 0)

    def test_collision_skipped(self):
        """If .txt already exists, the .sending file is left alone."""
        sending = self.results / "proactive-9999.sending"
        txt = self.results / "proactive-9999.txt"
        sending.write_text("orphan")
        txt.write_text("existing")
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 0, "collision should not count as recovered")
        self.assertTrue(sending.exists(), ".sending must remain on collision")
        self.assertEqual(txt.read_text(), "existing", "existing .txt must be untouched")

    def test_non_proactive_sending_files_ignored(self):
        """Only proactive-*.sending files are recovered; others are left."""
        (self.results / "task-abc123.sending").write_text("task")
        (self.results / "other.sending").write_text("other")
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 0)
        self.assertTrue((self.results / "task-abc123.sending").exists())
        self.assertTrue((self.results / "other.sending").exists())

    def test_mixed_files_only_proactive_recovered(self):
        (self.results / "proactive-111.sending").write_text("p")
        (self.results / "task-222.sending").write_text("t")
        (self.results / "proactive-333.txt").write_text("existing")
        n = self.mod._recover_orphan_sending_files()
        self.assertEqual(n, 1)
        self.assertTrue((self.results / "proactive-111.txt").exists())
        self.assertTrue((self.results / "task-222.sending").exists())


if __name__ == "__main__":
    unittest.main()

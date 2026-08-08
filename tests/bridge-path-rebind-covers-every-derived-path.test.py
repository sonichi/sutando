#!/usr/bin/env python3
"""rebind_workspace() must leave no bridge path attribute under the real workspace.

Discovery is by relationship to the resolved root rather than a name list, so a
constant added later is covered without anyone remembering to extend a list.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "_helpers"))
import bridge_paths  # noqa: E402


def _load_slack_bridge():
    """Same stub pattern tests/slack-bridge-tofu-enroll.test.py uses."""
    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace(chat_postMessage=lambda **kw: None)

        def event(self, _):
            return lambda fn: fn

    try:
        import slack_bolt as _real_bolt
        _real_bolt.App = _StubApp
    except ImportError:
        stub = types.ModuleType("slack_bolt")
        stub.App = _StubApp
        sys.modules["slack_bolt"] = stub
    if "slack_bolt.adapter" not in sys.modules:
        sys.modules["slack_bolt.adapter"] = types.ModuleType("slack_bolt.adapter")
    if "slack_bolt.adapter.socket_mode" not in sys.modules:
        sm = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm
    os.environ.setdefault("CLAUDE_CONFIG_DIR", tempfile.mkdtemp())
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-placeholder")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-placeholder")
    # deliberately NOT setting SUTANDO_WORKSPACE: the resolver ignores it, which is
    # the whole reason this helper exists. Isolation comes from rebind_workspace().
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_rebind_probe", REPO / "src" / "slack-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class RebindCoversEveryDerivedPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_slack_bridge()

    def test_discovery_finds_the_real_population(self):
        """A vacuous discovery would make every assertion below pass for free."""
        found = bridge_paths.derived_path_attrs(self.mod)
        self.assertGreaterEqual(
            len(found), 8,
            f"discovery found only {len(found)} derived paths; slack-bridge binds 9 at import",
        )
        for expected in ("STATE_DIR", "OWNER_ACTIVITY_FILE", "PENDING_REPLIES_FILE"):
            self.assertIn(expected, found, f"{expected} not discovered")

    def test_nothing_is_left_under_the_real_root(self):
        old_root = bridge_paths._original_root(self.mod)
        self.assertIsNotNone(old_root)
        with tempfile.TemporaryDirectory() as td:
            originals = bridge_paths.rebind_workspace(self.mod, Path(td))
            leaked = {}
            for n in dir(self.mod):
                if n.startswith("__"):
                    continue
                v = getattr(self.mod, n, None)
                if isinstance(v, Path) and str(v).startswith(str(old_root)):
                    leaked[n] = str(v)
            bridge_paths.restore(self.mod, originals)
        self.assertEqual(leaked, {}, f"still aimed at the live workspace: {leaked}")

    def test_the_member_2615_missed_is_covered(self):
        """PENDING_REPLIES_FILE — bound like OWNER_ACTIVITY_FILE, not rebound by #2615."""
        with tempfile.TemporaryDirectory() as td:
            originals = bridge_paths.rebind_workspace(self.mod, Path(td))
            got = str(self.mod.PENDING_REPLIES_FILE)
            bridge_paths.restore(self.mod, originals)
        self.assertTrue(got.startswith(td), f"PENDING_REPLIES_FILE still at {got}")

    def test_restore_puts_them_back(self):
        before = {n: str(v) for n, v in bridge_paths.derived_path_attrs(self.mod).items()}
        with tempfile.TemporaryDirectory() as td:
            originals = bridge_paths.rebind_workspace(self.mod, Path(td))
        bridge_paths.restore(self.mod, originals)
        after = {n: str(v) for n, v in bridge_paths.derived_path_attrs(self.mod).items()}
        self.assertEqual(before, after, "restore did not return the module to its original paths")


if __name__ == "__main__":
    unittest.main(verbosity=2)

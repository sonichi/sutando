#!/usr/bin/env python3
"""Worker-picker read path: pool-status carries room bindings with pinned
flags, and the writer pushes on change instead of waiting out the throttle.

Run: python3 tests/pool-status-bindings.test.py   (stdlib only)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "src" / "runtime-api"))
from pool_status import PoolStatusWriter  # noqa: E402


class PoolStatusBindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.clock = [1000.0]
        self.bindings = {}

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self):
        return PoolStatusWriter(
            self.tasks, self.state, lambda: ["core-1"],
            lambda i: True, now_fn=lambda: self.clock[0],
            bindings_fn=lambda: dict(self.bindings))

    def read(self):
        return json.loads((self.state / "pool-status.json").read_text())

    def test_snapshot_carries_bindings_with_pinned_flags(self):
        self.bindings = {
            "!room:x": {"instance": "core-1", "ts": 9.0, "pinned": True},
            "chan-d": {"instance": "core-2", "ts": 8.0},
            "junk": "not-a-row"}
        w = self.writer()
        self.assertTrue(w.maybe_write())
        got = self.read()["bindings"]
        self.assertEqual(got["!room:x"], {"instance": "core-1",
                                          "pinned": True,
                                          "dedicated": False})
        self.assertEqual(got["chan-d"], {"instance": "core-2",
                                         "pinned": False,
                                         "dedicated": False})
        self.assertNotIn("junk", got)

    def test_binding_change_writes_inside_the_throttle_window(self):
        w = self.writer()
        self.assertTrue(w.maybe_write())
        self.clock[0] += 5  # deep inside the 30s window
        self.bindings = {"!room:x": {"instance": "core-1", "pinned": True}}
        self.assertTrue(w.maybe_write(), "a pin must land immediately")
        self.assertEqual(self.read()["ts"], 1005)

    def test_unchanged_content_still_throttled(self):
        # negative control: push-on-change must not turn into write-always
        w = self.writer()
        self.assertTrue(w.maybe_write())
        self.clock[0] += 5
        self.assertFalse(w.maybe_write())
        self.assertEqual(self.read()["ts"], 1000)

    def test_heartbeat_write_after_window_without_change(self):
        w = self.writer()
        self.assertTrue(w.maybe_write())
        self.clock[0] += 31
        self.assertTrue(w.maybe_write(), "readers need a trustable ts")
        self.assertEqual(self.read()["ts"], 1031)

    def test_no_bindings_fn_keeps_legacy_shape(self):
        w = PoolStatusWriter(self.tasks, self.state, lambda: [],
                             lambda i: False, now_fn=lambda: self.clock[0])
        self.assertTrue(w.maybe_write())
        self.assertNotIn("bindings", self.read())


class DaemonWiringTest(unittest.TestCase):
    def test_daemon_injects_lead_bindings(self):
        src = (Path(__file__).resolve().parent.parent
               / "scripts" / "pool-lead-daemon.py").read_text()
        self.assertIn("bindings_fn=lead.bindings", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

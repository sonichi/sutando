"""check_hitl_archive: health-check surfaces a failing HITL archive step or a
regrown live store, which the gateway would otherwise re-read every second."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

from hitl import manager as hm, projector, schema  # noqa: E402


class HitlArchiveCheckTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ws = Path(td.name)
        orig = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws
        self.addCleanup(lambda: setattr(hc, "WORKSPACE_DIR", orig))
        self.root = hm.default_store(self.ws)

    def fill(self, n):
        self.root.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (self.root / f"hitl_{i:04d}.json").write_text("{}")

    def test_no_store_is_not_reported(self):
        self.assertIsNone(hc.check_hitl_archive())

    def test_a_small_healthy_store_is_ok(self):
        self.fill(3)
        out = hc.check_hitl_archive()
        self.assertEqual((out["name"], out["status"]), ("hitl-archive", "ok"))
        self.assertIn("3 live", out["detail"])

    def test_a_live_store_past_the_threshold_warns(self):
        self.fill(hc.HITL_LIVE_WARN + 1)
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn(f"{hc.HITL_LIVE_WARN + 1} live records", out["detail"])

    def test_published_archive_failures_warn(self):
        self.fill(1)
        (self.root / ".archive-status.json").write_text(
            json.dumps({"failures": 2, "error": "PermissionError: denied"}))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 settled record(s) failing to archive", out["detail"])

    def test_a_real_archive_failure_reaches_health(self):
        mgr = hm.HitlManager(hm.HitlStore(self.root))
        req = mgr.create(schema.HumanRequirement(
            kind="choice", runtime="claude", message="m", guard="g",
            actions=[schema.Action(id="ok", kind="choose", label="OK")]))
        mgr.resolve(req.id)
        mgr.record_projection(req.id, mgr.get(req.id).revision, "$ev")
        mgr.store.archive_root.write_text("not a dir")
        with self.assertLogs("hitl.store", level="WARNING"):
            projector.pending_ids(mgr)
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("1 settled record(s) failing to archive", out["detail"])

    def test_an_unreadable_status_file_degrades_to_the_live_count(self):
        self.fill(1)
        (self.root / ".archive-status.json").write_text("[not json")
        self.assertEqual(hc.check_hitl_archive()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

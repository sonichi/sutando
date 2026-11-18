"""check_hitl_archive: health-check surfaces a failing HITL archive step or a
regrown live store, which the gateway would otherwise re-read every second."""

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from unittest import mock
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

    def test_a_live_store_at_the_threshold_is_ok(self):
        self.fill(hc.HITL_LIVE_WARN)
        self.assertEqual(hc.check_hitl_archive()["status"], "ok")

    def test_a_live_store_past_the_threshold_warns(self):
        self.fill(hc.HITL_LIVE_WARN + 1)
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn(f"{hc.HITL_LIVE_WARN + 1} live records", out["detail"])

    def test_published_archive_failures_warn(self):
        self.fill(1)
        hm.archive_status_path(self.root).write_text(json.dumps(
            {"failures": 2, "error": "PermissionError: denied", "updated_at": time.time()}))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 settled record(s) failing to archive", out["detail"])

    def test_a_stale_failure_status_warns_as_stale(self):
        self.fill(1)
        hm.archive_status_path(self.root).write_text(json.dumps(
            {"failures": 1, "error": "PermissionError: denied",
             "updated_at": time.time() - hm.ARCHIVE_STATUS_STALE_S - 60}))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("archive status stale", out["detail"])

    def test_a_zero_failure_status_from_a_stopped_writer_warns_as_stale(self):
        self.fill(5)
        hm.archive_status_path(self.root).write_text(json.dumps(
            {"failures": 0, "error": "", "updated_at": time.time() - 86400}))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("archive status stale", out["detail"])

    def test_an_unreadable_store_warns_instead_of_raising(self):
        with mock.patch.object(hm, "archive_health", side_effect=PermissionError("denied")):
            out = hc.check_hitl_archive()
        self.assertEqual((out["name"], out["status"]), ("hitl-archive", "warn"))
        self.assertIn("hitl store unreadable", out["detail"])

    def test_run_all_checks_registers_the_row(self):
        sentinel = {"name": "hitl-archive", "status": "ok", "detail": "registration sentinel"}
        with mock.patch.object(hc, "check_hitl_archive", return_value=sentinel) as probe:
            checks = hc.run_all_checks()
        probe.assert_called_once_with()
        self.assertIn(sentinel, checks)

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

    def test_a_card_that_could_not_be_projected_warns(self):
        mgr = hm.HitlManager(hm.HitlStore(self.root))
        req = mgr.create(schema.HumanRequirement(
            kind="choice", runtime="claude", message="m", guard="g",
            actions=[schema.Action(id="ok", kind="choose", label="OK")]))

        def refuse(payload):
            err = OSError("HTTP 400")
            err.code = 400
            raise err

        self.addCleanup(projector._NO_ACCEPT_SINCE.clear)
        for i in range(projector.PROJECTION_MAX_REFUSALS + 1):  # the first falls inside the grace
            with self.assertRaises(OSError):  # a lone refusal is also re-raised for the caller's backoff
                projector.project_pass(mgr, refuse, "!r", clock=lambda: time.time() + 10 ** 7 * (i + 1),
                                       monotonic=lambda: time.monotonic() + 10 ** 7 * (i + 1))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn(f"1 card(s) could not be projected ({req.id})", out["detail"])

    def test_cards_sharing_a_refused_defect_warn_once_the_grace_runs_out(self):
        mgr = hm.HitlManager(hm.HitlStore(self.root))
        reqs = [mgr.create(schema.HumanRequirement(
            kind="choice", runtime="claude", message="m", guard=g,
            actions=[schema.Action(id="ok", kind="choose", label="OK")])) for g in ("g1", "g2")]
        self.addCleanup(projector._NO_ACCEPT_SINCE.clear)

        def refuse(payload):
            err = OSError("HTTP 400")
            err.code = 400
            raise err

        for i in range(projector.PROJECTION_MAX_REFUSALS + 1):
            with self.assertRaises(OSError):
                projector.project_pass(mgr, refuse, "!r", clock=lambda: time.time() + 10 ** 7 * (i + 1),
                                       monotonic=lambda: time.monotonic() + 10 ** 7 * (i + 1))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn("2 card(s) could not be projected", out["detail"])
        for req in reqs:
            self.assertIn(req.id, out["detail"])

    def test_a_failed_card_in_a_sibling_store_the_bridge_projects_warns(self):
        self.fill(1)
        sibling = self.root.with_name(self.root.name + "-claude-local")
        mgr = hm.HitlManager(hm.HitlStore(sibling))
        req = mgr.create(schema.HumanRequirement(
            kind="choice", runtime="claude-local", message="m", guard="g",
            actions=[schema.Action(id="ok", kind="choose", label="OK")]))

        def refuse(payload):
            err = OSError("HTTP 400")
            err.code = 400
            raise err

        self.addCleanup(projector._NO_ACCEPT_SINCE.clear)
        for i in range(projector.PROJECTION_MAX_REFUSALS + 1):  # the first falls inside the grace
            with self.assertRaises(OSError):  # a lone refusal is also re-raised for the caller's backoff
                projector.project_pass(mgr, refuse, "!r", clock=lambda: time.time() + 10 ** 7 * (i + 1),
                                       monotonic=lambda: time.monotonic() + 10 ** 7 * (i + 1))
        self.assertTrue(mgr.projection_failed(req.id))
        out = hc.check_hitl_archive()
        self.assertEqual(out["status"], "warn")
        self.assertIn(req.id, out["detail"])

    def test_an_unreadable_status_file_degrades_to_the_live_count(self):
        self.fill(1)
        hm.archive_status_path(self.root).write_text("[not json")
        self.assertEqual(hc.check_hitl_archive()["status"], "ok")


if __name__ == "__main__":
    unittest.main()

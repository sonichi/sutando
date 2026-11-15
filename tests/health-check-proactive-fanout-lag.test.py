#!/usr/bin/env python3
"""Regression for #5076: the ag2space proactive outbox stopped claiming at
06:09Z while the sibling discord outbox kept delivering, and nothing visible
said so — an item-level delivery check only reads the leg that actually
claimed an item, so it cannot see that a sibling leg went dark. This pins
`check_proactive_fanout_lag`, which compares each `.outbox-*-proactive` leg's
newest item write against its siblings' and flags one lagging far behind.

Run: python3 tests/health-check-proactive-fanout-lag.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_proactive_fanout_lag_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(hc)
    except SystemExit:
        pass
    return hc


class ProactiveFanoutLagProbe(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / "results").mkdir()

    def _item(self, root_name: str, item_id: str, age_sec: float = 0.0):
        import outbox
        root = self.ws / "results" / root_name
        outbox._write_item(root, item_id, {"item_id": item_id, "status": "DELIVERED"})
        path = root / outbox.ITEMS_DIR / f"{outbox._safe_key(item_id)}.json"
        stamp = time.time() - age_sec
        os.utime(path, (stamp, stamp))

    def test_no_proactive_roots_is_ok(self):
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no sibling", r["detail"])

    def test_a_lone_leg_has_no_sibling_to_compare(self):
        self._item(".outbox-ag2space-proactive", "proactive-1~a")
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no sibling", r["detail"])

    def test_non_proactive_outbox_roots_are_not_counted_as_siblings(self):
        self._item(".outbox-ag2space-proactive", "proactive-1~a")
        self._item(".outbox-slack-results", "task-1")
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no sibling", r["detail"])

    def test_two_fresh_legs_are_ok(self):
        self._item(".outbox-ag2space-proactive", "proactive-1~a")
        self._item(".outbox-discord-proactive", "proactive-1~a")
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("within", r["detail"])

    def test_both_legs_empty_is_ok(self):
        (self.ws / "results" / ".outbox-ag2space-proactive" / ".items").mkdir(parents=True)
        (self.ws / "results" / ".outbox-discord-proactive" / ".items").mkdir(parents=True)
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no items yet", r["detail"])

    def test_a_stalled_leg_warns_and_names_the_live_sibling(self):
        self._item(".outbox-discord-proactive", "proactive-2~b")  # fresh, ~now
        self._item(".outbox-ag2space-proactive", "proactive-1~a", age_sec=7200)  # 2h stale
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn(".outbox-ag2space-proactive", r["detail"])
        self.assertIn(".outbox-discord-proactive", r["detail"])

    def test_a_lag_under_threshold_is_ok(self):
        self._item(".outbox-discord-proactive", "proactive-2~b")
        self._item(".outbox-ag2space-proactive", "proactive-1~a", age_sec=600)  # 10m, under 1h
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")

    def test_a_custom_threshold_is_honored(self):
        self._item(".outbox-discord-proactive", "proactive-2~b")
        self._item(".outbox-ag2space-proactive", "proactive-1~a", age_sec=600)
        r = self.hc.check_proactive_fanout_lag(self.ws, lag_threshold_sec=60)
        self.assertEqual(r["status"], "warn")

    def test_both_legs_old_together_is_quiet_not_stalled(self):
        # Neither leg is active enough to prove the other has specifically
        # stopped; check_stale_proactive_backlog owns total silence.
        self._item(".outbox-discord-proactive", "proactive-2~b", age_sec=5 * 3600)
        self._item(".outbox-ag2space-proactive", "proactive-1~a", age_sec=5 * 3600 + 10)
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")
        self.assertIn("no leg is recent enough", r["detail"])

    def test_a_leg_abandoned_beyond_the_cap_is_not_flagged_forever(self):
        self._item(".outbox-discord-proactive", "proactive-2~b")
        self._item(".outbox-ag2space-proactive", "proactive-1~a", age_sec=40 * 86400)
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")

    def test_a_leg_that_never_held_an_item_is_not_flagged(self):
        self._item(".outbox-discord-proactive", "proactive-2~b")
        (self.ws / "results" / ".outbox-ag2space-proactive" / ".items").mkdir(parents=True)
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "ok")

    def test_an_unreadable_results_dir_is_unjudged_not_a_clean_zero(self):
        if sys.platform == "win32" or os.geteuid() == 0:
            self.skipTest("directory modes do not deny traversal here")
        self._item(".outbox-discord-proactive", "proactive-2~b")
        results = self.ws / "results"
        os.chmod(results, 0)
        self.addCleanup(os.chmod, results, 0o755)
        r = self.hc.check_proactive_fanout_lag(self.ws)
        self.assertEqual(r["status"], "warn")
        self.assertIn("unjudged", r["detail"])

    def test_the_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text(encoding="utf-8")
        self.assertIn("checks.append(check_proactive_fanout_lag())", src)

    def test_run_all_checks_registers_the_row(self):
        sentinel = {"name": "proactive-fanout-lag", "status": "ok",
                    "detail": "registration sentinel"}
        with unittest.mock.patch.object(self.hc, "check_proactive_fanout_lag",
                                        return_value=sentinel) as probe:
            checks = self.hc.run_all_checks()
        probe.assert_called_once_with()
        self.assertIn(sentinel, checks)


if __name__ == "__main__":
    unittest.main()

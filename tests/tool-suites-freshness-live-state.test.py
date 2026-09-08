#!/usr/bin/env python3
"""A suite whose FIXTURE is a live ledger must re-run when that ledger changes.

Freshness was computed over tool and suite mtimes only, so a hand-maintained
state file could change while the suite asserting over it stayed untouched --
and the suite was skipped exactly when its input moved. Measured: four rows in
`pr-flag-reviewed.json` recorded a head under `head` while every reader keys
`sha`, and the schema suite that catches it had been skipped as `fresh` for 5.8h.
"""
import importlib.util
import json
import pathlib
import tempfile
import time
import unittest

SRC = (pathlib.Path(__file__).resolve().parent.parent
       / "skills" / "proactive-loop" / "scripts" / "tool-suites-check.py")


def _load():
    spec = importlib.util.spec_from_file_location("tsc", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FreshnessSeesLiveInputs(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.d = pathlib.Path(tempfile.mkdtemp())

    def test_a_changed_ledger_is_an_input(self):
        led = self.d / "pr-flag-reviewed.json"
        led.write_text(json.dumps({"1": {"sha": "a"}}))
        self.assertIn(led, self.m.live_inputs(self.d))

    def test_the_scripts_own_sentinel_is_excluded(self):
        # Including it would make every run look changed, since this script writes it.
        (self.d / self.m.SENTINEL).write_text("{}")
        self.assertEqual(self.m.live_inputs(self.d), [])

    def test_a_ledger_edit_moves_the_freshness_clock(self):
        led = self.d / "pr-flag-reviewed.json"
        led.write_text("{}")
        before = self.m.newest_mtime(self.m.live_inputs(self.d))
        time.sleep(0.01)
        led.write_text('{"1": {"sha": "b"}}')
        after = self.m.newest_mtime(self.m.live_inputs(self.d))
        self.assertGreater(after, before, "editing a ledger did not move the clock")

    def test_should_run_fires_when_the_ledger_is_newer_than_the_last_run(self):
        state = {"tools_mtime": 100.0, "ran_at": 1000.0, "failed": []}
        go, why = self.m.should_run(state, 200.0, 6 * 3600, 1001.0)
        self.assertTrue(go)
        self.assertIn("changed", why)

    def test_an_unchanged_tree_still_reports_fresh(self):
        state = {"tools_mtime": 100.0, "ran_at": 1000.0, "failed": []}
        go, why = self.m.should_run(state, 100.0, 6 * 3600, 1001.0)
        self.assertFalse(go, "a genuinely unchanged tree must still skip")
        self.assertIn("fresh", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)

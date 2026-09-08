#!/usr/bin/env python3
"""A suite whose FIXTURE is a live ledger must re-run when that ledger changes.

Freshness was computed over tool and suite mtimes only, so a hand-maintained
state file could change while the suite asserting over it stayed untouched --
and the suite was skipped exactly when its input moved.

The ledgers are DECLARED, not globbed: `state/` also holds service heartbeats
rewritten every few seconds, and a glob makes `newest` always ~now, which
removes the freshness skip entirely instead of tightening it.
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


class FreshnessSeesDeclaredLedgers(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.d = pathlib.Path(tempfile.mkdtemp())

    def _declare(self, *names):
        (self.d / self.m.EXTRAS).write_text(json.dumps({"ledgers": list(names)}))

    def test_a_declared_ledger_that_exists_is_an_input(self):
        (self.d / "pr-flag-reviewed.json").write_text("{}")
        self._declare("pr-flag-reviewed.json")
        self.assertEqual([p.name for p in self.m.live_inputs(self.d, self.d)],
                         ["pr-flag-reviewed.json"])

    def test_an_undeclared_heartbeat_is_not_an_input(self):
        (self.d / "gateway-status.json").write_text("{}")
        self._declare("pr-flag-reviewed.json")
        self.assertEqual(self.m.live_inputs(self.d, self.d), [])

    def test_newest_is_STABLE_while_undeclared_telemetry_churns(self):
        # The property the previous tests could not see: they passed `newest`
        # straight into should_run, so a churning state dir never reached it.
        led = self.d / "pr-flag-reviewed.json"
        led.write_text("{}")
        beat = self.d / "gateway-status.json"
        beat.write_text("{}")
        self._declare("pr-flag-reviewed.json")
        before = self.m.newest_mtime(self.m.live_inputs(self.d, self.d))
        for _ in range(3):
            time.sleep(0.01)
            beat.write_text('{"t": 1}')
        after = self.m.newest_mtime(self.m.live_inputs(self.d, self.d))
        self.assertEqual(before, after,
                         "a heartbeat moved `newest`; the freshness skip is gone")

    def test_a_declared_ledger_edit_DOES_move_newest(self):
        led = self.d / "pr-flag-reviewed.json"
        led.write_text("{}")
        self._declare("pr-flag-reviewed.json")
        before = self.m.newest_mtime(self.m.live_inputs(self.d, self.d))
        time.sleep(0.01)
        led.write_text('{"1": {"sha": "b"}}')
        after = self.m.newest_mtime(self.m.live_inputs(self.d, self.d))
        self.assertGreater(after, before, "editing a declared ledger did not move the clock")

    def test_the_scripts_own_sentinel_cannot_be_declared(self):
        # Declaring it would make every run see a newer input, since this
        # script writes it -- a permanent re-run loop that looks like diligence.
        (self.d / self.m.SENTINEL).write_text("{}")
        self._declare(self.m.SENTINEL)
        self.assertEqual(self.m.live_inputs(self.d, self.d), [])

    def test_no_declaration_means_no_live_inputs(self):
        (self.d / "pr-flag-reviewed.json").write_text("{}")
        self.assertEqual(self.m.live_inputs(self.d, self.d), [])

    def test_a_non_list_ledgers_key_raises_rather_than_silently_empty(self):
        (self.d / self.m.EXTRAS).write_text(json.dumps({"ledgers": "nope"}))
        with self.assertRaises(self.m.ExtrasError):
            self.m.live_inputs(self.d, self.d)


if __name__ == "__main__":
    unittest.main(verbosity=2)

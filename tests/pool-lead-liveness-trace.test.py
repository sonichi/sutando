#!/usr/bin/env python3
"""Liveness trace: change-driven live-set lines + anomaly events for owner
picks that land on the lane core or fire while the bound core looks dead.
Forensic aid for the 2026-08-26 sticky-steal incident (cause unproven)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
sys.path.insert(0, str(REPO / "src"))
from pool_lead import PoolLead  # noqa: E402


class TraceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"; self.tasks.mkdir()
        self.state = root / "state"; self.state.mkdir()
        self.alive = {"core-1": True, "core-2": True, "core-3": True}
        self.clock = [1000.0]
        self.lead = PoolLead(self.tasks, self.state,
                             followers_fn=lambda: list(self.alive),
                             alive_fn=lambda i: self.alive.get(i, False),
                             now_fn=lambda: self.clock[0])

    def tearDown(self):
        self.tmp.cleanup()

    def _lines(self):
        p = self.state / "pool" / "liveness-trace.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()]


class ChangeDriven(TraceBase):
    def test_stable_live_set_writes_once(self):
        for _ in range(5):
            self.clock[0] += 2
            self.lead.sweep()
        events = [e for e in self._lines() if e["event"] == "live_set_changed"]
        self.assertEqual(len(events), 1)  # first observation only

    def test_blip_and_recovery_write_two_lines(self):
        self.lead.sweep()
        self.alive["core-1"] = False
        self.clock[0] += 2; self.lead.sweep()
        self.alive["core-1"] = True
        self.clock[0] += 2; self.lead.sweep()
        events = [e for e in self._lines() if e["event"] == "live_set_changed"]
        self.assertEqual(len(events), 3)
        self.assertNotIn("core-1", events[1]["alive"])
        self.assertIn("core-1", events[2]["alive"])


class AnomalyEvents(TraceBase):
    def test_owner_pick_while_bound_core_dead_is_traced(self):
        (self.state / "pool").mkdir(exist_ok=True)
        (self.state / "pool" / "affinity.json").write_text(
            json.dumps({"chan-A": {"instance": "core-1", "ts": 1.0}}))
        self.alive["core-1"] = False
        (self.tasks / "task-o1.txt").write_text(
            "id: task-o1\nchannel_id: chan-A\ntask: t\n")
        self.lead.sweep()
        anomalies = [e for e in self._lines()
                     if e["event"] == "anomalous_owner_pick"]
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0]["bound"], "core-1")
        self.assertNotIn("core-1", anomalies[0]["alive"])

    def test_normal_bound_pick_writes_no_anomaly(self):
        (self.state / "pool").mkdir(exist_ok=True)
        (self.state / "pool" / "affinity.json").write_text(
            json.dumps({"chan-A": {"instance": "core-1", "ts": 1.0}}))
        (self.tasks / "task-o2.txt").write_text(
            "id: task-o2\nchannel_id: chan-A\ntask: t\n")
        self.lead.sweep()
        self.assertEqual(
            [e for e in self._lines()
             if e["event"] == "anomalous_owner_pick"], [])



class ExplicitPinToLaneCoreIsNotAnomalous(TraceBase):
    def test_bound_lane_core_pick_writes_no_anomaly(self):
        # explicit pin to the lane core is a deliberate binding, not a misroute
        (self.state / "pool").mkdir(exist_ok=True)
        (self.state / "pool" / "affinity.json").write_text(
            '{"C7": {"instance": "core-3", "ts": 999.0}}')
        (self.tasks / "task-p1.txt").write_text(
            "id: task-p1\nsource: slack\nchannel_id: C7\n"
            "access_tier: owner\npriority: normal\ntask: hi\n")
        self.lead.sweep()
        anomalies = [e for e in self._lines()
                     if e["event"] == "anomalous_owner_pick"]
        self.assertEqual(anomalies, [])
        hits = [f.name for f in self.tasks.iterdir()
                if f.name.startswith("task-p1.assigned-")]
        self.assertEqual(hits, ["task-p1.assigned-core-3.txt"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

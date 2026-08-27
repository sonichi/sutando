#!/usr/bin/env python3
"""quota_projection: writer contract (append/dedup/cap) + chart series math.

The series invariant under test is the one the chart's legibility rests on:
every segment is one reset span normalized to x∈[0,1] with the even-pace
line y=x, so over-use is exactly y>x and a sample outside its own window is
dropped rather than plotted into a neighbouring segment.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "src"))
import quota_projection as qp  # noqa: E402

SPAN5 = qp.WINDOW_SPANS["5h"]
SPAN7 = qp.WINDOW_SPANS["7d"]


def state(u5="0.25", r5=1000000, u7="0.55", r7=2000000):
    return {"headers": {
        "anthropic-ratelimit-unified-5h-utilization": u5,
        "anthropic-ratelimit-unified-5h-reset": str(r5),
        "anthropic-ratelimit-unified-7d-utilization": u7,
        "anthropic-ratelimit-unified-7d-reset": str(r7),
    }}


class RecordSample(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "quota-history.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_appends_a_parsed_sample(self):
        self.assertTrue(qp.record_sample(state(), self.path, now=999000.0))
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(rec, {"ts": 999000.0, "u5": 0.25, "r5": 1000000,
                               "u7": 0.55, "r7": 2000000})

    def test_identical_values_do_not_grow_the_file(self):
        qp.record_sample(state(), self.path, now=1.0)
        self.assertFalse(qp.record_sample(state(), self.path, now=2.0))
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_a_changed_utilization_is_a_new_sample(self):
        qp.record_sample(state(), self.path, now=1.0)
        self.assertTrue(qp.record_sample(state(u5="0.26"), self.path, now=2.0))
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_missing_headers_record_nothing(self):
        self.assertFalse(qp.record_sample({"headers": {}}, self.path, now=1.0))
        self.assertFalse(qp.record_sample({}, self.path, now=1.0))
        self.assertFalse(self.path.exists())

    def test_cap_keeps_the_newest_tail(self):
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 3
        try:
            for i in range(5):
                qp.record_sample(state(u5=f"0.{10+i}"), self.path, now=float(i))
            lines = [json.loads(x) for x in self.path.read_text().splitlines()]
            self.assertEqual(len(lines), 3)
            self.assertEqual([r["ts"] for r in lines], [2.0, 3.0, 4.0])
        finally:
            qp.MAX_LINES = old_max

    def test_a_torn_tail_line_is_skipped_not_fatal(self):
        qp.record_sample(state(), self.path, now=1.0)
        with self.path.open("a") as f:
            f.write('{"ts": 2.0, "u5": 0.3')  # crash mid-write
        # Dedup keys on the last GOOD line; the changed sample must then
        # survive a fresh read (torn tail repaired, not concatenated onto).
        self.assertFalse(qp.record_sample(state(), self.path, now=3.0))
        self.assertTrue(qp.record_sample(state(u5="0.30"), self.path, now=4.0))
        recs = qp._read_history(self.path)
        self.assertEqual(recs[-1]["u5"], 0.30)
        self.assertEqual(recs[-1]["ts"], 4.0)

    def test_stagnant_usage_projects_from_now_not_the_frozen_point(self):
        # Reviewer's control: 25% @ 10% elapsed, same 25% re-polled at 60%
        # (dedup'd), rendered — projection must be 0.25/0.6, never 0.25/0.1.
        now0 = 1000000.0
        span = SPAN5
        reset = int(now0 + span * 0.9)          # first poll at 10% elapsed
        qp.record_sample(state(u5="0.25", r5=reset, u7="0.1", r7=reset),
                         self.path, now=now0)
        later = reset - span + span * 0.6       # 60% elapsed, usage unchanged
        self.assertFalse(qp.record_sample(
            state(u5="0.25", r5=reset, u7="0.1", r7=reset), self.path, now=later))
        seg = qp.chart_payload(self.path, now=later)["windows"]["5h"]["segments"][0]
        self.assertAlmostEqual(seg["projected_end"], 0.25 / 0.6, places=3)
        self.assertAlmostEqual(seg["points"][-1]["x"], 0.6, places=3)
        self.assertAlmostEqual(seg["points"][-1]["y"], 0.25)

    def test_concurrent_writers_lose_no_acknowledged_sample(self):
        # Production-writer concurrency at the cap boundary: every thread
        # whose record_sample returned True must find its sample in the file.
        import threading
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 8
        try:
            for i in range(7):                  # near the cap
                qp.record_sample(state(u5=f"0.{100+i}"), self.path, now=float(i))
            acked = []
            lock = threading.Lock()
            def worker(i):
                s = state(u5=f"0.{200+i}")
                if qp.record_sample(s, self.path, now=float(100 + i)):
                    with lock:
                        acked.append(float(100 + i))
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
            for th in threads: th.start()
            for th in threads: th.join()
            kept = {r["ts"] for r in qp._read_history(self.path)}
            lost = [ts for ts in acked if ts not in kept]
            self.assertEqual(lost, [], f"acknowledged samples lost: {lost}")
        finally:
            qp.MAX_LINES = old_max


class ChartSeries(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "quota-history.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def write(self, recs):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def test_x_is_the_elapsed_fraction_of_the_window(self):
        reset = 1000000
        ts = reset - SPAN5 // 2  # halfway through the 5h window
        self.write([{"ts": ts, "u5": 0.4, "r5": reset, "u7": 0.1, "r7": reset}])
        seg = qp.chart_payload(self.path, now=float(reset + 1))["windows"]["5h"]["segments"][0]
        self.assertAlmostEqual(seg["points"][0]["x"], 0.5)
        self.assertAlmostEqual(seg["points"][0]["y"], 0.4)
        self.assertFalse(seg["current"])

    def test_over_use_is_y_above_x(self):
        # 60% used at 25% elapsed — over pace; the chart colors on y > x.
        reset = 1000000
        ts = reset - int(SPAN5 * 0.75)
        self.write([{"ts": ts, "u5": 0.6, "r5": reset, "u7": 0.1, "r7": reset}])
        pt = qp.chart_payload(self.path, now=float(reset + 1))["windows"]["5h"]["segments"][0]["points"][0]
        self.assertGreater(pt["y"], pt["x"])

    def test_each_reset_becomes_its_own_segment_in_order(self):
        r1, r2 = 1000000, 1000000 + SPAN5
        self.write([
            {"ts": r1 - 100, "u5": 0.9, "r5": r1, "u7": 0.1, "r7": r1},
            {"ts": r2 - 100, "u5": 0.2, "r5": r2, "u7": 0.1, "r7": r1},
        ])
        segs = qp.chart_payload(self.path, now=float(r2 + 1))["windows"]["5h"]["segments"]
        self.assertEqual([s["reset"] for s in segs], [r1, r2])

    def test_a_sample_outside_its_window_is_dropped(self):
        reset = 1000000
        self.write([{"ts": reset + 50, "u5": 0.4, "r5": reset, "u7": 0.1, "r7": reset}])
        self.assertEqual(
            qp.chart_payload(self.path, now=float(reset + 100))["windows"]["5h"]["segments"], [])

    def test_current_window_carries_a_pace_projection(self):
        now = 1000000.0
        reset = int(now + SPAN5 // 2)          # halfway through, still current
        ts = int(now)                          # x = 0.5
        self.write([{"ts": ts, "u5": 0.4, "r5": reset, "u7": 0.1, "r7": reset}])
        seg = qp.chart_payload(self.path, now=now)["windows"]["5h"]["segments"][0]
        self.assertTrue(seg["current"])
        self.assertAlmostEqual(seg["projected_end"], 0.8)  # 0.4 / 0.5

    def test_max_windows_keeps_the_newest(self):
        resets = [1000000 + i * SPAN5 for i in range(6)]
        self.write([{"ts": r - 100, "u5": 0.5, "r5": r, "u7": 0.1, "r7": resets[0]}
                    for r in resets])
        segs = qp.chart_payload(self.path, now=float(resets[-1] + 1),
                                max_windows=4)["windows"]["5h"]["segments"]
        self.assertEqual([s["reset"] for s in segs], resets[-4:])

    def test_missing_history_file_yields_empty_windows(self):
        payload = qp.chart_payload(self.path, now=1.0)
        self.assertEqual(payload["windows"]["5h"]["segments"], [])
        self.assertEqual(payload["windows"]["7d"]["segments"], [])


class DashboardWiring(unittest.TestCase):
    """The dashboard delegates; it does not re-own the history writer."""

    def test_route_and_sampler_delegate_to_the_module(self):
        src = (REPO / "src" / "dashboard.py").read_text()
        self.assertIn("quota_projection.record_sample(", src)
        self.assertIn("quota_projection.chart_payload(", src)
        self.assertIn('"/api/quota-chart"', src)

    def test_dashboard_does_not_write_the_history_file_itself(self):
        src = (REPO / "src" / "dashboard.py").read_text()
        # The only mentions of the history file must be arguments to the
        # module's functions — never an open()/write of its own.
        for line in src.splitlines():
            if "quota-history" in line:
                self.assertIn("quota_projection.", src[max(0, src.index(line) - 200):src.index(line) + len(line)],
                              f"history path used outside the module call: {line.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

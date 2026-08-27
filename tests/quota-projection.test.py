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


def state(u5="0.25", r5=None, u7="0.55", r7=None, obs=999000.0):
    """A quota-state dict whose `last_checked` IS the observation time (obs).

    Resets default to mid-window relative to obs — the validator refuses an
    observation outside its own window, so fixtures stay self-consistent.
    """
    from datetime import datetime, timezone
    lc = datetime.fromtimestamp(obs, timezone.utc).isoformat().replace("+00:00", "Z")
    if r5 is None:
        r5 = int(obs + 9000)
    if r7 is None:
        r7 = int(obs + 300000)
    h = {}
    if u5 is not None:
        h["anthropic-ratelimit-unified-5h-utilization"] = u5
        h["anthropic-ratelimit-unified-5h-reset"] = str(r5)
    if u7 is not None:
        h["anthropic-ratelimit-unified-7d-utilization"] = u7
        h["anthropic-ratelimit-unified-7d-reset"] = str(r7)
    return {"last_checked": lc, "headers": h}


class RecordSample(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "quota-history.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_appends_a_parsed_sample(self):
        self.assertTrue(qp.record_sample(state(), self.path, now=999000.0))
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual(rec, {"ts": 999000.0, "u5": 0.25, "r5": 1008000,
                               "u7": 0.55, "r7": 1299000})

    def test_identical_values_do_not_grow_the_file(self):
        qp.record_sample(state(), self.path)
        self.assertFalse(qp.record_sample(state(), self.path))
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_a_changed_utilization_is_a_new_sample(self):
        qp.record_sample(state(obs=1000.0), self.path)
        self.assertTrue(qp.record_sample(state(u5="0.26", obs=1060.0), self.path))
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
                qp.record_sample(state(u5=f"0.{10+i}", obs=float(i)), self.path)
            lines = [json.loads(x) for x in self.path.read_text().splitlines()]
            self.assertEqual(len(lines), 3)
            self.assertEqual([r["ts"] for r in lines], [2.0, 3.0, 4.0])
        finally:
            qp.MAX_LINES = old_max

    def test_a_torn_tail_line_is_skipped_not_fatal(self):
        qp.record_sample(state(obs=1.0), self.path)
        with self.path.open("a") as f:
            f.write('{"ts": 2.0, "u5": 0.3')
        # Crash-torn tail: dedup keys on the last GOOD line, and the changed
        # sample must survive a fresh read (tail repaired, not appended onto).
        self.assertFalse(qp.record_sample(state(obs=1.0), self.path))
        self.assertTrue(qp.record_sample(state(u5="0.30", obs=4.0), self.path))
        recs = qp._read_history(self.path)
        self.assertEqual(recs[-1]["u5"], 0.30)
        self.assertEqual(recs[-1]["ts"], 4.0)

    def test_a_reread_of_the_same_snapshot_never_advances_the_observation(self):
        # Reviewer control (#3464): 25% observed at 10% elapsed; the UNCHANGED
        # file reread at 60% must keep fraction 0.10 — no observation happened.
        span = SPAN5
        reset = 1000000 + span
        obs = reset - span + span * 0.1
        st = state(u5="0.25", r5=reset, u7="0.1", r7=reset, obs=obs)
        self.assertTrue(qp.record_sample(st, self.path))
        self.assertFalse(qp.record_sample(st, self.path))  # same last_checked
        seg = qp.chart_payload(self.path, now=reset - span + span * 0.6)["windows"]["5h"]["segments"][0]
        self.assertAlmostEqual(seg["points"][-1]["x"], 0.1, places=3)
        self.assertAlmostEqual(seg["projected_end"], 2.0)  # capped: over at observation

    def test_order_is_judged_against_the_last_valid_predecessor(self):
        # 4th-round control: [ts=2000, ts="bad"] + matching obs at 1500 —
        # drop the corrupt row, reject 1500 against the valid 2000.
        import json as js
        R = 1000000
        rows = [
            {"ts": 995000.0, "u5": 0.25, "r5": R, "u7": 0.55, "r7": R},
            {"ts": "bad", "u5": 0.25, "r5": R, "u7": 0.55, "r7": R},
        ]
        self.path.write_text("".join(js.dumps(r) + "\n" for r in rows))
        self.assertFalse(qp.record_sample(state(obs=990000.0, r5=R, r7=R), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)], [995000.0])
        # a later CHANGED sample behind the valid tail is also refused
        self.assertFalse(qp.record_sample(state(u5="0.30", obs=993000.0, r5=R, r7=R), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)], [995000.0])
        # and one genuinely newer lands normally
        self.assertTrue(qp.record_sample(state(u5="0.30", obs=998000.0, r5=R, r7=R), self.path))

    def test_an_infinite_trailing_timestamp_cannot_poison_the_future(self):
        import json as js
        self.path.write_text(js.dumps(
            {"ts": "Infinity", "u5": 0.25, "r5": 1000000, "u7": 0.55, "r7": 1000000}) + "\n")
        self.assertTrue(qp.record_sample(state(obs=3000.0), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)], [3000.0])

    def test_an_absurd_finite_timestamp_cannot_freeze_the_writer(self):
        # 5th-round control: ts=1e100 is finite but outside every window it
        # claims — it is an invalid row, never a canonical tail.
        import json as js
        self.path.write_text(js.dumps(
            {"ts": 1e100, "u5": 0.25, "r5": 1000000, "u7": 0.55, "r7": 1000000}) + "\n")
        self.assertTrue(qp.record_sample(state(obs=1700000000.0), self.path))
        self.assertTrue(qp.record_sample(state(u5="0.30", obs=1700000600.0), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)],
                         [1700000000.0, 1700000600.0])

    def test_a_correlated_absurd_row_cannot_freeze_the_writer(self):
        # 6th-round control: ts=r5=r7=1e100 is self-consistent under float
        # precision (1e100-18000 == 1e100); the caller's clock refuses it.
        import json as js
        self.path.write_text(js.dumps(
            {"ts": 1e100, "u5": 0.25, "r5": 1e100, "u7": 0.55, "r7": 1e100}) + "\n")
        self.assertTrue(qp.record_sample(state(obs=1700000000.0), self.path))
        self.assertTrue(qp.record_sample(state(u5="0.30", obs=1700000600.0), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)],
                         [1700000000.0, 1700000600.0])

    def test_the_writer_never_acknowledges_a_row_its_reader_refuses(self):
        # 6th-round idempotence contract: a fractional reset cannot
        # canonicalize losslessly -> refuse, never a True with empty read-back.
        ok = qp.record_sample(
            state(obs=1000.5, r5="1000.9", r7="1000.9"), self.path)
        self.assertFalse(ok)
        self.assertFalse(self.path.exists())

    def test_an_overflowing_integer_is_invalid_not_fatal(self):
        # json.loads accepts arbitrarily huge ints; float() raises
        # OverflowError on them — that must read as invalid, not crash.
        import json as js
        self.path.write_text(
            '{"ts": ' + "9" * 400 + ', "u5": 0.25, "r5": 1000000, "u7": 0.55, "r7": 1000000}\n')
        self.assertTrue(qp.record_sample(state(obs=5000.0), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)], [5000.0])

    def test_fully_nonfinite_utilization_records_nothing(self):
        # Qingyun's control: NaN/Inf utilization in every window -> False,
        # nothing written, and the file never grows on re-poll.
        for bad in ("NaN", "Infinity", "-Infinity"):
            self.assertFalse(qp.record_sample(
                state(u5=bad, u7=bad, obs=1000.0), self.path))
        self.assertFalse(self.path.exists())

    def test_a_poisoned_window_does_not_sink_the_valid_one(self):
        # Per-window persistence (5th round): NaN in 5h leaves a valid 7d-only
        # row; the stored JSON is strict (no bare NaN anywhere).
        self.assertTrue(qp.record_sample(
            state(u5="NaN", u7="0.55", obs=2000.0), self.path))
        raw = self.path.read_text()
        self.assertNotIn("NaN", raw)
        rec = qp._read_history(self.path)[0]
        self.assertNotIn("u5", rec)
        self.assertEqual(rec["u7"], 0.55)

    def test_a_single_window_observation_persists_its_window(self):
        # The proxy may write any non-empty header subset; a 5h-only
        # observation must not vanish.
        self.assertTrue(qp.record_sample(
            state(u5="0.25", u7=None, obs=3000.0), self.path))
        rec = qp._read_history(self.path)[0]
        self.assertEqual(rec["u5"], 0.25)
        self.assertNotIn("u7", rec)

    def test_a_corrupt_final_timestamp_is_repaired_not_fatal(self):
        # Reviewer control (#3464, 2nd round): a final row with ts="bad" and
        # matching values must never raise out of the sampler; it is repaired.
        import json as js
        self.path.write_text(js.dumps(
            {"ts": "bad", "u5": 0.25, "r5": 1000000, "u7": 0.55, "r7": 1000000}) + "\n")
        self.assertTrue(qp.record_sample(state(obs=5000.0), self.path))
        recs = qp._read_history(self.path)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[-1]["ts"], 5000.0)

    def test_a_corrupt_final_timestamp_survives_the_real_panel_path(self):
        # Same control through get_quota_status(): the panel must keep its
        # headers, and repeated polls must not repeat a failure.
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp()); (tmp / "state").mkdir()
        from datetime import datetime, timezone
        lc = datetime.fromtimestamp(1700000000.0, timezone.utc).isoformat().replace("+00:00", "Z")
        (tmp / "state" / "quota-state.json").write_text(js.dumps({
            "available": True, "last_checked": lc,
            "headers": {
                "anthropic-ratelimit-unified-5h-utilization": "0.25",
                "anthropic-ratelimit-unified-5h-reset": "1700016200",
                "anthropic-ratelimit-unified-7d-utilization": "0.55",
                "anthropic-ratelimit-unified-7d-reset": "1700500000"}}))
        (tmp / "state" / "quota-history.jsonl").write_text(js.dumps(
            {"ts": "bad", "u5": 0.25, "r5": 1700016200, "u7": 0.55, "r7": 1700500000}) + "\n")
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            for _ in range(2):
                q = dashboard.get_quota_status()
                self.assertTrue(q.get("headers"), "one corrupt row must not blank the panel")
        finally:
            dashboard.WORKSPACE_DIR = old_ws

    def test_an_unchanged_run_keeps_its_first_endpoint_and_advances_its_last(self):
        # Reviewer control (#3464, 3rd round): 25% verified at x=.1 then at
        # x=.6 must yield BOTH endpoints — the early over-pace point survives.
        span = SPAN5
        reset = 1000000 + span
        def st(frac): return state(u5="0.25", r5=reset, u7="0.1", r7=reset,
                                   obs=reset - span + span * frac)
        self.assertTrue(qp.record_sample(st(0.1), self.path))
        self.assertTrue(qp.record_sample(st(0.6), self.path))   # run end appended
        self.assertFalse(qp.record_sample(st(0.8), self.path))  # end advanced in place
        recs = qp._read_history(self.path)
        self.assertEqual([round((r["ts"] - (reset - span)) / span, 2) for r in recs],
                         [0.1, 0.8])
        seg = qp.chart_payload(self.path, now=float(reset - 1))["windows"]["5h"]["segments"][0]
        self.assertAlmostEqual(seg["points"][0]["x"], 0.1, places=2)
        self.assertAlmostEqual(seg["points"][-1]["x"], 0.8, places=2)
        self.assertAlmostEqual(seg["projected_end"], 0.25 / 0.8, places=3)

    def test_a_delayed_older_snapshot_never_lands(self):
        # Reviewer control: newer committed first, stale arrives late, newer
        # rereads — the file must hold producer order, no stale append.
        span = SPAN5
        reset = 1000000 + span
        newer = state(u5="0.30", r5=reset, u7="0.1", r7=reset, obs=reset - span + span * 0.6)
        stale = state(u5="0.20", r5=reset, u7="0.1", r7=reset, obs=reset - span + span * 0.1)
        self.assertTrue(qp.record_sample(newer, self.path))
        self.assertFalse(qp.record_sample(stale, self.path))   # rejected: ts <= tail
        self.assertFalse(qp.record_sample(newer, self.path))   # reread: no-op
        recs = qp._read_history(self.path)
        self.assertEqual([(round(r["ts"]), r["u5"]) for r in recs],
                         [(round(reset - span + span * 0.6), 0.30)])


    def test_concurrent_writers_lose_no_acknowledged_sample(self):
        # Production-writer concurrency at the cap boundary: every thread
        # whose record_sample returned True must find its sample in the file.
        import threading
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 8
        try:
            for i in range(7):                  # near the cap
                qp.record_sample(state(u5=f"0.{100+i}", obs=float(i)), self.path)
            acked = []
            lock = threading.Lock()
            def worker(i):
                s = state(u5=f"0.{200+i}", obs=float(100 + i))
                if qp.record_sample(s, self.path):
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

    def test_a_malformed_record_is_skipped_not_fatal(self):
        reset = 1000000
        self.write([
            {"ts": "not-a-number", "u5": 0.5, "r5": reset, "u7": 0.1, "r7": reset},
            {"ts": reset - 100, "u5": 0.4, "r5": reset, "u7": 0.1, "r7": reset},
        ])
        segs = qp.chart_payload(self.path, now=float(reset + 1))["windows"]["5h"]["segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(segs[0]["points"]), 1)

    def test_missing_history_file_yields_empty_windows(self):
        payload = qp.chart_payload(self.path, now=1.0)
        self.assertEqual(payload["windows"]["5h"]["segments"], [])
        self.assertEqual(payload["windows"]["7d"]["segments"], [])


class DashboardAdapter(unittest.TestCase):
    """The live route and the sampler's failure swallow, on the real handler."""

    def test_quota_chart_route_serves_the_payload(self):
        import http.client
        import http.server
        import threading
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp()); (tmp / "state").mkdir()
        (tmp / "state" / "quota-history.jsonl").write_text(
            js.dumps({"ts": 999000, "u5": 0.4, "r5": 1000000, "u7": 0.2, "r7": 1000000}) + "\n")
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/quota-chart")
            r = c.getresponse()
            body = js.loads(r.read())
            httpd.shutdown()
            self.assertEqual(r.status, 200)
            self.assertEqual(len(body["windows"]["5h"]["segments"]), 1)
        finally:
            dashboard.WORKSPACE_DIR = old_ws

    def test_a_nonfinite_payload_yields_500_not_an_empty_200(self):
        # 6th-round control: strict-JSON failure must surface BEFORE headers.
        import http.client
        import http.server
        import threading
        import json as js
        import dashboard
        real = dashboard.quota_projection.chart_payload
        dashboard.quota_projection.chart_payload = lambda *a, **k: {"x": float("nan")}
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/quota-chart")
            r = c.getresponse()
            body = r.read().decode()
            httpd.shutdown()
            self.assertEqual(r.status, 500)
            self.assertNotIn("NaN", body)
            js.loads(body)  # the error body itself is strict JSON
        finally:
            dashboard.quota_projection.chart_payload = real

    def test_a_poisoned_history_file_still_serves_strict_json(self):
        # A hand-poisoned bare-NaN row must never reach the wire: the
        # canonical reader drops it and the route stays parseable JSON.
        import http.client
        import http.server
        import threading
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp()); (tmp / "state").mkdir()
        (tmp / "state" / "quota-history.jsonl").write_text(
            '{"ts": 999000.0, "u5": NaN, "r5": 1008000, "u7": 0.55, "r7": 1299000}\n'
            + js.dumps({"ts": 999100.0, "u5": 0.3, "r5": 1008000, "u7": 0.55, "r7": 1299000}) + "\n")
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            c.request("GET", "/api/quota-chart")
            r = c.getresponse()
            body = r.read().decode()
            httpd.shutdown()
            self.assertEqual(r.status, 200)
            self.assertNotIn("NaN", body)
            js.loads(body)  # strict parse must succeed
        finally:
            dashboard.WORKSPACE_DIR = old_ws

    def test_recorded_time_is_the_producers_not_the_routes(self):
        # Reviewer control through the REAL get_quota_status() path: the stored
        # ts must equal last_checked; a reread of the same file adds nothing.
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp())
        (tmp / "state").mkdir()
        obs = 1700000000.0
        from datetime import datetime, timezone
        lc = datetime.fromtimestamp(obs, timezone.utc).isoformat().replace("+00:00", "Z")
        (tmp / "state" / "quota-state.json").write_text(js.dumps({
            "available": True, "last_checked": lc,
            "headers": {
                "anthropic-ratelimit-unified-5h-utilization": "0.25",
                "anthropic-ratelimit-unified-5h-reset": str(int(obs + 16200)),
                "anthropic-ratelimit-unified-7d-utilization": "0.55",
                "anthropic-ratelimit-unified-7d-reset": str(int(obs + 500000))}}))
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            dashboard.get_quota_status()
            dashboard.get_quota_status()  # reread, hours of route time later
        finally:
            dashboard.WORKSPACE_DIR = old_ws
        recs = qp._read_history(tmp / "state" / "quota-history.jsonl")
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(recs[0]["ts"], obs, places=1)

    def test_sampler_failure_never_breaks_the_quota_panel(self):
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp())
        (tmp / "state").mkdir()
        (tmp / "state" / "quota-state.json").write_text(js.dumps({
            "available": True, "last_checked": "2026-08-27T00:00:00.000Z",
            "headers": {
                "anthropic-ratelimit-unified-5h-utilization": "0.25",
                "anthropic-ratelimit-unified-5h-reset": "1000000",
                "anthropic-ratelimit-unified-7d-utilization": "0.55",
                "anthropic-ratelimit-unified-7d-reset": "2000000"}}))
        # history path unwritable: a FILE occupies the parent dir name
        (tmp / "state" / "quota-history.jsonl").mkdir()  # open("a") -> IsADirectoryError(OSError)
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            q = dashboard.get_quota_status()
            self.assertTrue(q.get("headers"), "panel data must survive a sampler OSError")
        finally:
            dashboard.WORKSPACE_DIR = old_ws


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

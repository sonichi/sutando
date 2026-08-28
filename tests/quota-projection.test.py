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

    def test_negative_utilization_never_reaches_the_wire(self):
        # 7th-round control: u5=-1e308 forged projected_end=-inf and a 500.
        # A negative utilization now invalidates its window at ingest.
        self.assertFalse(qp.record_sample(
            state(u5="-1e308", u7=None, obs=2000.0), self.path))
        # the observation lands only as a data-free tombstone marker
        self.assertNotIn("u5", self.path.read_text())
        # and a stored negative row is refused by the reader
        import json as js
        self.path.write_text(js.dumps(
            {"ts": 2000.0, "u5": -0.2, "r5": 9000, "u7": 0.55, "r7": 300000}) + "\n")
        recs = qp._read_history(self.path, now=3000.0)
        self.assertEqual([k for r in recs for k in r if k == "u5"], [])

    def test_the_clock_bound_is_two_sided(self):
        # 7th-round control: ts=r5=-1e100 was still a canonical row.
        import json as js
        self.path.write_text(js.dumps(
            {"ts": -1e100, "u5": 0.25, "r5": -1e100, "u7": 0.55, "r7": -1e100}) + "\n")
        self.assertTrue(qp.record_sample(state(obs=1700000000.0), self.path))
        self.assertEqual([r["ts"] for r in qp._read_history(self.path)], [1700000000.0])

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

    def test_the_cap_holds_on_the_refresh_path_too(self):
        # Reviewer control: an over-cap file + a newer same-value observation
        # took the early-return and left the file over cap.
        import json as js
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 5
        try:
            R = 1000000
            rows = [{"ts": 990000.0 + i, "u5": round(0.1 + i * 0.01, 3),
                     "r5": R, "u7": 0.5, "r7": R} for i in range(7)]
            rows[-1]["u5"] = rows[-2]["u5"]     # last two form an unchanged run
            self.path.write_text("".join(js.dumps(r) + "\n" for r in rows))
            st = state(u5=str(rows[-1]["u5"]), r5=R, u7="0.5", r7=R,
                       obs=rows[-1]["ts"] + 60)
            self.assertFalse(qp.record_sample(st, self.path))  # refresh path
            n = len(self.path.read_text().splitlines())
            self.assertLessEqual(n, qp.MAX_LINES, f"file still over cap: {n}")
        finally:
            qp.MAX_LINES = old_max

    def test_max_lines_one_keeps_exactly_one(self):
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 1
        try:
            for i in range(3):
                qp.record_sample(state(u5=f"0.{10 + i}", obs=1000.0 + i), self.path)
            self.assertEqual(len(self.path.read_text().splitlines()), 1)
        finally:
            qp.MAX_LINES = old_max

    def test_cross_process_acks_all_survive(self):
        # Reviewer control: six PROCESSES (not threads) write concurrently;
        # every acked sample must be in the final file (cap not in play).
        import subprocess
        import sys as _sys
        procs = []
        for i in range(6):
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "import quota_projection as qp\n"
                "from pathlib import Path\n"
                "import tests_helper_state as h\n"
            ) % str(REPO / "src")
            # inline the state builder instead of a helper import
            code = (
                "import sys, json; sys.path.insert(0, %r)\n"
                "import quota_projection as qp\n"
                "from pathlib import Path\n"
                "from datetime import datetime, timezone\n"
                "obs = 1000.0 + %d\n"
                "lc = datetime.fromtimestamp(obs, timezone.utc).isoformat().replace('+00:00','Z')\n"
                "st = {'last_checked': lc, 'headers': {\n"
                " 'anthropic-ratelimit-unified-5h-utilization': '0.%d',\n"
                " 'anthropic-ratelimit-unified-5h-reset': str(int(obs+9000)),\n"
                " 'anthropic-ratelimit-unified-7d-utilization': '0.5',\n"
                " 'anthropic-ratelimit-unified-7d-reset': str(int(obs+300000))}}\n"
                "print(int(qp.record_sample(st, Path(%r))))\n"
            ) % (str(REPO / "src"), i, 20 + i, str(self.path))
            procs.append(subprocess.Popen([_sys.executable, "-c", code],
                                          stdout=subprocess.PIPE, text=True))
        acks = []
        for i, pr in enumerate(procs):
            out, _ = pr.communicate(timeout=60)
            if out.strip() == "1":
                acks.append(i)
        recs = qp._read_history(self.path)
        kept_ts = {r["ts"] for r in recs}
        for i in acks:
            self.assertIn(1000.0 + i, kept_ts,
                          f"acked sample {i} missing — ack was not durable")

    def test_an_unreadable_existing_history_refuses_the_write(self):
        import os as _os
        qp.record_sample(state(obs=1000.0), self.path)
        _os.chmod(self.path, 0)
        try:
            got = qp.record_sample(state(u5="0.9", obs=2000.0), self.path)
        finally:
            _os.chmod(self.path, 0o600)
        self.assertFalse(got, "an unreadable history must refuse, not fork")
        self.assertEqual(len(qp._read_history(self.path)), 1)

    def test_the_writer_never_acknowledges_a_row_its_reader_refuses(self):
        # 6th-round idempotence contract: a fractional reset cannot
        # canonicalize losslessly -> refuse, never a True with empty read-back.
        ok = qp.record_sample(
            state(obs=1000.5, r5="1000.9", r7="1000.9"), self.path)
        self.assertFalse(ok)
        # read-back yields no window data; only the tombstone marker remains
        recs = qp._read_history(self.path, now=2000.0)
        self.assertEqual([k for r in recs for k in r if k not in ("ts", "tomb")], [])

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
        # one tombstone marker from the first refusal; re-polls at the same
        # ts add nothing, and no utilization value ever reaches the file
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("u5", lines[0])

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


    def test_sidecar_loss_cannot_regress_current_to_an_older_window(self):
        # Reviewer control: no sidecar (every pre-upgrade history), a delayed
        # older observation must not claim "current" over the newer tail.
        import json as js
        rows = [{"ts": 900000.0, "u5": 0.20, "r5": 909000, "u7": 0.5, "r7": 909000},
                {"ts": 900100.0, "u5": 0.30, "r5": 909100, "u7": 0.5, "r7": 909100}]
        self.path.write_text("".join(js.dumps(r) + "\n" for r in rows))
        self.assertFalse(qp._latest_path(self.path).exists())
        delayed = state(u5="0.20", r5=909000, u7="0.5", r7=909000, obs=900000.0)
        self.assertFalse(qp.record_sample(delayed, self.path, now=900200.0))
        segs = qp.chart_payload(self.path, now=900200.0)["windows"]["5h"]["segments"]
        cur = [s["reset"] for s in segs if s["current"]]
        self.assertNotIn(909000, cur, f"older window presented as current: {cur}")
        self.assertEqual([s["reset"] for s in segs], [909000, 909100])
        fresh = state(u5="0.31", r5=909200, u7="0.5", r7=909200, obs=900150.0)
        self.assertTrue(qp.record_sample(fresh, self.path, now=900200.0))
        segs = qp.chart_payload(self.path, now=900200.0)["windows"]["5h"]["segments"]
        self.assertEqual([s["reset"] for s in segs if s["current"]][0], 909200)

    def test_a_fully_invalid_observation_still_applies_the_cap(self):
        # Reviewer control: the writer owns the physical cap on EVERY path,
        # including the early return for an observation with no valid window.
        import json as js
        old_max = qp.MAX_LINES
        qp.MAX_LINES = 5
        try:
            rows = [{"ts": 990000.0 + i, "u5": round(0.1 + i * 0.01, 3),
                     "r5": 1000000, "u7": 0.5, "r7": 1000000} for i in range(7)]
            self.path.write_text("".join(js.dumps(r) + "\n" for r in rows))
            obs = 990066.0
            bad = state(u5="0.3", r5=int(obs) - 1, u7="0.5", r7=int(obs) - 1, obs=obs)
            self.assertFalse(qp.record_sample(bad, self.path, now=obs))
            n = len(self.path.read_text().splitlines())
            self.assertLessEqual(n, qp.MAX_LINES, f"file still over cap: {n}")
            rows = qp._read_history(self.path, obs)
            # the marker takes the tail slot (correctness outranks one point)
            self.assertEqual([r["ts"] for r in rows if not r.get("tomb")],
                             [990003.0, 990004.0, 990005.0, 990006.0])
            self.assertTrue(rows[-1].get("tomb"))
            self.assertEqual(rows[-1]["ts"], obs)
        finally:
            qp.MAX_LINES = old_max

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
        # Through the WRITER, so the latest-observation sidecar defines current.
        now = 1000000.0
        reset = int(now + SPAN5 // 2)          # halfway through, still current
        qp.record_sample(state(u5="0.4", r5=reset, u7="0.1", r7=int(now + 300000),
                               obs=now), self.path)
        seg = qp.chart_payload(self.path, now=now)["windows"]["5h"]["segments"][0]
        self.assertTrue(seg["current"])
        self.assertAlmostEqual(seg["projected_end"], 0.8)  # 0.4 / 0.5

    def test_history_alone_is_never_current(self):
        # Direct-written rows with no latest-observation sidecar: rendered as
        # history, but nothing is current and nothing projects.
        now = 1000000.0
        reset = int(now + SPAN5 // 2)
        self.write([{"ts": int(now), "u5": 0.4, "r5": reset, "u7": 0.1, "r7": reset}])
        seg = qp.chart_payload(self.path, now=now)["windows"]["5h"]["segments"][0]
        self.assertFalse(seg["current"])
        self.assertNotIn("projected_end", seg)

    def test_a_tombstoned_window_has_nothing_current(self):
        # Reviewer control: newer observation with NaN 5h + valid 7d — the old
        # 5h curve must stop presenting as current; 7d stays live.
        now0 = 1000000.0
        r5 = int(now0 + 9000); r7 = int(now0 + 300000)
        qp.record_sample(state(u5="0.8", r5=r5, u7="0.35", r7=r7, obs=now0), self.path)
        qp.record_sample(state(u5="NaN", r5=r5, u7="0.4", r7=r7, obs=now0 + 60), self.path)
        pay = qp.chart_payload(self.path, now=now0 + 120)
        cur5 = [s for s in pay["windows"]["5h"]["segments"] if s["current"]]
        cur7 = [s for s in pay["windows"]["7d"]["segments"] if s["current"]]
        self.assertEqual(cur5, [], "stale 5h presented as current past its tombstone")
        self.assertEqual(len(cur7), 1)
        self.assertNotIn("projected_end", pay["windows"]["5h"]["segments"][0])

    def test_a_fully_invalid_observation_tombstones_both_windows(self):
        now0 = 1000000.0
        qp.record_sample(state(obs=now0), self.path)
        self.assertFalse(qp.record_sample(
            state(u5="NaN", u7="NaN", obs=now0 + 60), self.path))
        pay = qp.chart_payload(self.path, now=now0 + 120)
        for w in ("5h", "7d"):
            self.assertEqual([s for s in pay["windows"][w]["segments"] if s["current"]], [])

    def test_current_follows_the_newer_observation_when_resets_shrink(self):
        # Reviewer control: old ts with a FURTHER reset must not out-shout a
        # newer observation whose reset is nearer.
        now0 = 1000000.0
        qp.record_sample(state(u5="0.8", r5=int(now0 + 11000), u7="0.1",
                               r7=int(now0 + 300000), obs=now0), self.path)
        qp.record_sample(state(u5="0.1", r5=int(now0 + 5000), u7="0.1",
                               r7=int(now0 + 300000), obs=now0 + 100), self.path)
        segs = qp.chart_payload(self.path, now=now0 + 200)["windows"]["5h"]["segments"]
        cur = [s for s in segs if s["current"]]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["reset"], int(now0 + 5000))

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

    def test_a_huge_finite_utilization_cannot_sink_the_page(self):
        # 8th-round control: v=1e308 is finite and nonnegative; v*100 is inf
        # and int(inf) raised through render_dashboard, taking the whole page.
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp()); (tmp / "state").mkdir()
        from datetime import datetime, timezone
        lc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        (tmp / "state" / "quota-state.json").write_text(js.dumps({
            "available": True, "last_checked": lc,
            "headers": {
                "anthropic-ratelimit-unified-5h-utilization": "1e308",
                "anthropic-ratelimit-unified-5h-reset": "1900000000",
                "anthropic-ratelimit-unified-7d-utilization": "0.55",
                "anthropic-ratelimit-unified-7d-reset": "1900000000"}}))
        old_ws = dashboard.WORKSPACE_DIR
        old_stats = dashboard.get_system_stats
        dashboard.WORKSPACE_DIR = tmp
        # Platform-neutral seam: stub the pmset/disk probes but keep the REAL
        # get_quota_status call, so the quota path stays activated on Linux CI.
        dashboard.get_system_stats = lambda: {
            "disk_free": "1GB", "battery": "—", "charging": False,
            "uptime": "00:00", "quota": dashboard.get_quota_status()}
        try:
            html = dashboard.render_dashboard()  # the ACTIVATED path, unguarded before
        finally:
            dashboard.WORKSPACE_DIR = old_ws
            dashboard.get_system_stats = old_stats
        self.assertIn("999%+", html)
        self.assertIn("55%", html)

    def test_a_bad_5h_reset_never_sinks_the_valid_7d_panel(self):
        # 7th-round control: bad 5h reset + valid 7d degraded the whole panel
        # to {"available": True}; now only the bad window goes unknown.
        import tempfile as tf
        import json as js
        import dashboard
        tmp = Path(tf.mkdtemp()); (tmp / "state").mkdir()
        from datetime import datetime, timezone
        lc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        (tmp / "state" / "quota-state.json").write_text(js.dumps({
            "available": True, "last_checked": lc,
            "headers": {
                "anthropic-ratelimit-unified-5h-utilization": "0.25",
                "anthropic-ratelimit-unified-5h-reset": "oops",
                "anthropic-ratelimit-unified-7d-utilization": "0.55",
                "anthropic-ratelimit-unified-7d-reset": "1900000000"}}))
        old_ws = dashboard.WORKSPACE_DIR
        dashboard.WORKSPACE_DIR = tmp
        try:
            q = dashboard.get_quota_status()
        finally:
            dashboard.WORKSPACE_DIR = old_ws
        self.assertTrue(q.get("headers"), "panel data must survive one bad reset")
        self.assertIn("reset_7d", q)
        self.assertNotIn("reset_5h", q)

    def test_tiles_render_unknown_not_zero_or_crash(self):
        # 7th-round control: a 5h-only reading showed 7d as 0%; NaN/oops in a
        # utilization crashed the whole page render.
        import dashboard
        base = {"available": True, "headers": {
            "anthropic-ratelimit-unified-5h-utilization": "0.26"}}
        self.assertEqual(dashboard._quota_tile_pct(base, "5h"), "26%")
        self.assertEqual(dashboard._quota_tile_pct(base, "7d"), "—")
        for bad in ("NaN", "Infinity", "oops", "-0.5", None):
            poisoned = {"available": True, "headers": {
                "anthropic-ratelimit-unified-5h-utilization": bad,
                "anthropic-ratelimit-unified-7d-utilization": "0.55"}}
            self.assertEqual(dashboard._quota_tile_pct(poisoned, "5h"), "—", bad)
            self.assertEqual(dashboard._quota_tile_pct(poisoned, "7d"), "55%", bad)

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



class Round12Controls(unittest.TestCase):
    """kewei round-9 P1s + qingyun's ts-refusal cap — each fails at 7e88ceec."""

    def test_tombstone_survives_sidecar_loss(self):
        # valid 990000 -> tombstone 990200 -> sidecar deleted -> delayed
        # 990100 must be REJECTED: the marker row is the high-water mark.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"
            now = 990300.0
            self.assertTrue(qp.record_sample(
                state(50, 999000, 40, 1200000, 990000), hp, now=now))
            self.assertFalse(qp.record_sample(
                state(None, None, None, None, 990200.0), hp, now=now))
            qp._latest_path(hp).unlink()
            self.assertFalse(qp.record_sample(
                state(60, 999100, 45, 1200000, 990100), hp, now=now))
            rows = qp._read_history(hp, now=now)
            self.assertNotIn(990100.0, [r["ts"] for r in rows])
            payload = qp.chart_payload(hp, now=now)
            self.assertEqual(
                [s for s in payload["windows"]["5h"]["segments"]
                 if s["current"]], [])

    def test_truncation_keeps_the_newest_smaller_reset(self):
        # Five stale larger resets + a newer smaller one: magnitude-keyed
        # truncation dropped reset 1600 (current=[]); recency keying keeps it.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"
            now = 1560.0
            fixtures = ((17500, 100), (18000, 200), (18500, 600),
                        (19000, 1100), (19500, 1501))
            for i, (reset, ts) in enumerate(fixtures):
                self.assertTrue(qp.record_sample(
                    state(10 + i, reset, None, None, float(ts)),
                    hp, now=now))
            self.assertTrue(qp.record_sample(
                state(30, 1600, None, None, 1550.0), hp, now=now))
            payload = qp.chart_payload(hp, now=now)
            cur = [s for s in payload["windows"]["5h"]["segments"]
                   if s["current"]]
            self.assertEqual([s["reset"] for s in cur], [1600])

    def test_an_expired_reset_is_never_current_or_projected(self):
        # First sample lands long after its own reset passed: history yes,
        # current/projection no.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"
            self.assertTrue(qp.record_sample(
                state(72, 101000, None, None, 100000), hp, now=100001.0))
            payload = qp.chart_payload(hp, now=200000.0)
            segs = payload["windows"]["5h"]["segments"]
            self.assertTrue(segs)
            self.assertFalse(any(s["current"] for s in segs))
            self.assertFalse(any("projected_end" in s for s in segs))

    def test_missing_timestamp_still_pays_the_cap(self):
        # qingyun P1: the ts-refusal path owes the same locked cap/compaction.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"
            now = 990100.0
            rows = [json.dumps({"ts": 989000.0 + i, "u5": 1.0 + i,
                                "r5": 990000 + i}) for i in range(7)]
            hp.write_text("\n".join(rows) + "\n")
            old = qp.MAX_LINES
            qp.MAX_LINES = 5
            try:
                self.assertFalse(qp.record_sample(
                    {"headers": {}}, hp, now=now))
                self.assertLessEqual(
                    len(hp.read_text().splitlines()), 5)
                self.assertFalse(qp.record_sample(
                    {"headers": {}, "last_checked": float("nan")}, hp,
                    now=now))
                self.assertLessEqual(
                    len(hp.read_text().splitlines()), 5)
            finally:
                qp.MAX_LINES = old



class Round12AcceptanceCriteria(unittest.TestCase):
    """kewei's fixed acceptance shapes (2026-08-28): the tombstone is a
    high-water mark, never a permanent seal, and it survives compaction;
    truncation choice and current-hood are separate axes."""

    def _tombstone_sequence(self, hp, now):
        self.assertTrue(qp.record_sample(
            state(50, 999000, 40, 1200000, 990000), hp, now=now))
        self.assertFalse(qp.record_sample(
            state(None, None, None, None, 990200.0), hp, now=now))
        qp._latest_path(hp).unlink()
        self.assertFalse(qp.record_sample(
            state(60, 999100, 45, 1200000, 990100), hp, now=now))

    def test_a_later_valid_sample_clears_the_tombstone(self):
        # High-water, not a seal: 990300 after the reject sequence is a
        # normal accept, and its window becomes current again.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"; now = 990400.0
            self._tombstone_sequence(hp, now)
            self.assertTrue(qp.record_sample(
                state(70, 999300, 50, 1200000, 990300), hp, now=now))
            payload = qp.chart_payload(hp, now=now)
            cur = [s["reset"] for s in payload["windows"]["5h"]["segments"]
                   if s["current"]]
            self.assertEqual(cur, [999300])

    def test_the_rejection_holds_after_compaction(self):
        # Same sequence with the cap forcing a compaction rewrite first:
        # compaction must not resurrect the pre-tombstone current.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"; now = 990400.0
            old = qp.MAX_LINES
            qp.MAX_LINES = 2
            try:
                self._tombstone_sequence(hp, now)
                self.assertLessEqual(len(hp.read_text().splitlines()), 2)
                self.assertFalse(qp.record_sample(
                    state(60, 999100, 45, 1200000, 990100), hp, now=now))
                payload = qp.chart_payload(hp, now=now)
                self.assertEqual(
                    [s for s in payload["windows"]["5h"]["segments"]
                     if s["current"]], [])
            finally:
                qp.MAX_LINES = old

    def test_widening_max_windows_does_not_change_current_identity(self):
        # max_windows=5 shows one more HISTORY segment; current stays [1600].
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"; now = 1560.0
            for i, (reset, ts) in enumerate(
                    ((17500, 100), (18000, 200), (18500, 600),
                     (19000, 1100), (19500, 1501))):
                self.assertTrue(qp.record_sample(
                    state(10 + i, reset, None, None, float(ts)), hp, now=now))
            self.assertTrue(qp.record_sample(
                state(30, 1600, None, None, 1550.0), hp, now=now))
            for mw in (4, 5):
                payload = qp.chart_payload(hp, now=now, max_windows=mw)
                segs = payload["windows"]["5h"]["segments"]
                self.assertEqual(
                    [s["reset"] for s in segs if s["current"]], [1600],
                    f"max_windows={mw}")
            self.assertEqual(len(qp.chart_payload(hp, now=now, max_windows=5)
                                 ["windows"]["5h"]["segments"]), 5)

    def test_an_open_window_still_projects(self):
        # Anti-overcorrection: same shape as the expired control but with
        # now < reset — exactly one current, projection present.
        with tempfile.TemporaryDirectory() as d:
            hp = Path(d) / "h.jsonl"
            self.assertTrue(qp.record_sample(
                state(72, 101000, None, None, 100000), hp, now=100001.0))
            payload = qp.chart_payload(hp, now=100500.0)
            cur = [s for s in payload["windows"]["5h"]["segments"]
                   if s["current"]]
            self.assertEqual(len(cur), 1)
            self.assertIn("projected_end", cur[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

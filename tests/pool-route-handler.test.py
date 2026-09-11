#!/usr/bin/env python3
"""The router as the core watcher's handler: which exit code, and why.

The exit code IS the routing decision — the watcher acts on nothing else — so
each branch is pinned against the rule it encodes rather than against a number.

Run: python3 tests/pool-route-handler.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402
import pool_route_handler as h  # noqa: E402

W = "a" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True, exist_ok=True)

    def roster(self, state="live", label=None, bindings=None):
        row = {"state": state}
        if label:
            row["label"] = label
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: row},
             "bindings": bindings if bindings is not None else {"!room:x": W}}))

    def task_file(self, name, **headers):
        p = self.ws / "tasks" / f"{name}.txt"
        lines = [f"id: {name}"] + [f"{k}: {v}" for k, v in headers.items()]
        p.write_text("\n".join(lines) + "\ntask: body\n")
        return str(p)


class TestClassification(Base):
    def test_unbound_declines_so_the_core_takes_it(self):
        self.roster(bindings={})
        t = self.task_file("task-1", channel_id="!other:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_a_live_bound_worker_is_accepted(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)

    def test_a_target_not_on_the_roster_goes_to_the_core(self):
        """A name that was never created is not a routing failure: the core is
        a real recipient, and holding would strand the work indefinitely."""
        self.roster(bindings={"!room:x": "f" * 32})
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_a_non_live_target_is_still_delivered_to(self):
        """Delivery is the router's whole job; the sentinel is durable, so a
        worker that starts later finds its work. Liveness is separate logic."""
        self.roster(state="draining")
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_an_absent_roster_refuses_rather_than_declining(self):
        """Declining is the core. An unreadable file must not choose a recipient."""
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.TAKE)

    def test_a_corrupt_roster_refuses_too(self):
        (self.ws / "state" / "roster.json").write_text("{broken")
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.TAKE)


class TestIntentionLayer(Base):
    def test_a_requested_label_reaches_the_worker(self):
        self.roster(label="worker-1", bindings={})
        t = self.task_file("task-1", channel_id="!unbound:x", requested_worker="worker-1")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)
        h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_an_unknown_requested_worker_goes_to_the_core(self):
        """The addressed name is still never substituted BY ANOTHER WORKER --
        it goes to the core, and no worker receives work it was not named for."""
        self.roster(bindings={})
        t = self.task_file("task-1", channel_id="!room:x", requested_worker="worker-9")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())


class TestDelivery(Base):
    def test_the_real_run_delivers_and_leaves_the_payload(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        s = self.ws / "deliveries" / W / "task-1.txt"
        self.assertTrue(s.exists())
        self.assertEqual(s.stat().st_size, 0)
        self.assertTrue(Path(t).exists(), "the payload is never moved or copied")

    def test_a_task_mid_gateway_file_routes_to_the_core(self):
        """A writer that puts `task:` first declares no header the router may
        read; the core takes it, and the writer is what has to converge."""
        self.roster()
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\npriority: normal\ntask: Is worker working now?\n"
                     "source: ag2space\nchannel_id: !room:x\nsender_name: qingyun\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_a_body_still_cannot_forge_requested_worker_under_lenient_reading(self):
        self.roster(bindings={})
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\ntask: body\nrequested_worker: " + W + "\nchannel_id: !unbound:x\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.DECLINE)

    def test_the_body_is_not_read_as_headers(self):
        """`task:` is the last header, so a body cannot forge requested_worker."""
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\nchannel_id: !room:x\ntask: body\nrequested_worker: f" + "f" * 31 + "\n")
        self.roster()
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.TAKE)


class TestBodyCannotSelectTheRecipient(Base):
    """kewei's P1 on #4110: a lenient fallback let untrusted body text supply
    `channel_id`/`source`, so the BODY picked the worker."""

    def log(self):
        p = self.ws / "logs" / "pool-route-handler.log"
        return p.read_text() if p.exists() else ""

    def canonical(self, name, headers, body):
        """The canonical task-last writer, not a hand-built file."""
        p = self.ws / "tasks" / f"{name}.txt"
        p.write_text(ltp.serialize_task_last(headers, body))
        return p

    def test_a_body_forged_channel_id_does_not_select_a_worker(self):
        self.roster()
        p = self.canonical("task-1", [("id", "task-1"), ("source", "health-check")],
                           "look at this\nchannel_id: !room:x\n")
        self.assertIsNone(ltp.parse_task_headers(p.read_text()).headers.get("channel_id"))
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws)]), h.DECLINE)
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_a_body_forged_source_does_not_select_a_worker(self):
        self.roster(bindings={"health-check": W})
        p = self.canonical("task-1", [("id", "task-1")], "look at this\nsource: health-check\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws)]), h.DECLINE)
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_a_headerless_task_says_why_it_went_to_the_core(self):
        self.roster()
        p = self.canonical("task-1", [("id", "task-1")], "body\nchannel_id: !room:x\n")
        h.main(["--task-file", str(p), "--workspace", str(self.ws)])
        self.assertIn("no channel_id header", self.log())

    def test_a_real_header_still_routes(self):
        """The control: the same channel in the HEADER reaches the worker, so
        the cases above measure the parse and not a broken roster."""
        self.roster()
        p = self.canonical("task-1", [("id", "task-1"), ("channel_id", "!room:x")], "body")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]),
                         h.TAKE)


class TestOneRosterPerRun(Base):
    """kewei's P1 on #4110: classify() and _deliver() each loaded the roster,
    so one run could decide against two versions."""

    def rosters(self, *versions):
        """A loader that hands out a different roster on each call."""
        seq = iter(versions)
        last = [None]

        def loader(_ws):
            try:
                last[0] = next(seq)
            except StopIteration:
                pass
            return last[0]
        return unittest.mock.patch.object(h.pr, "load_roster", side_effect=loader)

    def test_a_roster_swapped_mid_run_does_not_change_the_delivery_set(self):
        bound = {"version": 1, "workers": {W: {"state": "live"}}, "bindings": {"!room:x": W}}
        unbound = {"version": 2, "workers": {W: {"state": "live"}}, "bindings": {}}
        t = self.task_file("task-1", channel_id="!room:x")
        with self.rosters(bound, unbound):
            self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())
        self.assertFalse((self.ws / "deliveries" / "core" / "task-1.txt").exists())

    def test_the_run_loads_the_roster_exactly_once(self):
        bound = {"version": 7, "workers": {W: {"state": "live"}}, "bindings": {"!room:x": W}}
        t = self.task_file("task-1", channel_id="!room:x")
        with self.rosters(bound) as loader:
            h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.assertEqual(loader.call_count, 1)

    def test_a_real_run_without_a_roster_defers_instead_of_delivering(self):
        """An unreadable roster picks no recipient: the run marks the task and
        exits 0, so the watcher does not hand a worker's task to the core."""
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "state" / "pool-route-retry" / "task-1").exists())
        self.assertIn("refused: roster is absent or unreadable", (self.ws / "logs" / "pool-route-handler.log").read_text())

    def test_the_run_line_names_the_version_it_decided_against(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.assertIn("roster=v1", (self.ws / "logs" / "pool-route-handler.log").read_text())


class TestRetryDriver(Base):
    """kewei's P1 on #4110: `_defer()` marked work for a retry pass that
    nothing in production ran. Every real run is now that driver."""

    def log(self):
        p = self.ws / "logs" / "pool-route-handler.log"
        return p.read_text() if p.exists() else ""

    def mark(self, task_id, channel="!room:x"):
        d = self.ws / "state" / "pool-route-retry"
        d.mkdir(parents=True, exist_ok=True)
        (d / task_id).write_text("refused: earlier\n")
        self.task_file(task_id, channel_id=channel)

    def test_the_next_handler_run_redelivers_a_marked_task(self):
        self.roster()
        self.mark("task-old")
        t = self.task_file("task-new", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-old.txt").exists())
        self.assertFalse((self.ws / "state" / "pool-route-retry" / "task-old").exists())
        self.assertIn("retry task-old: delivered", self.log())

    def test_a_marker_whose_payload_is_gone_is_dropped_with_a_log_line(self):
        self.roster()
        self.mark("task-old")
        (self.ws / "tasks" / "task-old.txt").unlink()
        t = self.task_file("task-new", channel_id="!room:x")
        h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.assertFalse((self.ws / "state" / "pool-route-retry" / "task-old").exists())
        self.assertIn("retry task-old: payload gone", self.log())

    def test_the_bound_holds_so_the_waking_task_is_still_handled(self):
        self.roster()
        for i in range(4):
            self.mark(f"task-old{i}")
        t = self.task_file("task-new", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws),
                                 "--retry-limit", "3"]), 0)
        done = [i for i in range(4)
                if not (self.ws / "state" / "pool-route-retry" / f"task-old{i}").exists()]
        self.assertEqual(len(done), 3)
        self.assertTrue((self.ws / "deliveries" / W / "task-new.txt").exists())

    def test_a_crash_in_the_pass_still_routes_the_waking_task(self):
        """The driver is opportunistic; it must never cost the task that
        triggered it."""
        self.roster()
        t = self.task_file("task-new", channel_id="!room:x")
        with unittest.mock.patch.object(h, "retry_pass", side_effect=RuntimeError("boom")):
            self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-new.txt").exists())
        self.assertIn("retry pass crashed", self.log())

    def test_the_probe_runs_no_retry_pass(self):
        """The probe is the watcher's read-only question; it must not deliver."""
        self.roster()
        self.mark("task-old")
        t = self.task_file("task-new", channel_id="!room:x")
        h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"])
        self.assertTrue((self.ws / "state" / "pool-route-retry" / "task-old").exists())



class TestFailureAfterTheProbe(Base):
    def log(self):
        p = self.ws / "logs" / "pool-route-handler.log"
        return p.read_text() if p.exists() else ""

    def test_a_refused_pass_defers_and_exits_zero(self):
        """A failed delivery keeps the task the worker's: a retry marker, exit 0
        (so the watcher never hands it to the core), the reason in the log."""
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}, "f" * 32: {"state": "live"}},
             "bindings": {"!room:x": [W, "f" * 32]}}))
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertIn("refused", self.log())
        self.assertTrue((self.ws / "state" / "pool-route-retry" / "task-1").exists())
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_the_retry_pass_delivers_a_deferred_task_and_clears_its_marker(self):
        """The fault is repaired by re-delivery, not by the core answering."""
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}, "f" * 32: {"state": "live"}},
             "bindings": {"!room:x": [W, "f" * 32]}}))
        t = self.task_file("task-1", channel_id="!room:x")
        h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.roster()  # the fault is fixed: one target again
        out = h.retry_pass(str(self.ws))
        self.assertEqual(out["delivered"], ["task-1"])
        self.assertFalse((self.ws / "state" / "pool-route-retry" / "task-1").exists())
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_the_retry_pass_drops_a_marker_whose_payload_is_gone(self):
        self.roster()
        d = self.ws / "state" / "pool-route-retry"; d.mkdir(parents=True)
        (d / "task-9").write_text("refused: x\n")
        self.assertEqual(h.retry_pass(str(self.ws))["gone"], ["task-9"])
        self.assertFalse((d / "task-9").exists())

    def test_a_task_archived_before_the_run_is_nothing_to_route(self):
        """Seen live: the worker finished and the bridge archived the payload
        between the probe and the run; exit 1 then sent the task to the core."""
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        Path(t).unlink()
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertIn("gone before the run", self.log())


class TestRunAndDeferral(Base):
    """`run()` is the diagnostic instrument: every exit of the real run leaves
    a log line, and a deferral never turns into a non-zero exit."""

    def log(self):
        p = self.ws / "logs" / "pool-route-handler.log"
        return p.read_text() if p.exists() else ""

    def test_a_normal_run_logs_the_code_it_returns(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.run(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertIn("run returning rc=0", self.log())

    def test_a_probe_logs_nothing_about_its_return(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        h.run(["--task-file", t, "--workspace", str(self.ws), "--probe"])
        self.assertNotIn("run returning", self.log())

    def test_an_exception_outside_main_is_logged_and_re_raised(self):
        with unittest.mock.patch.object(h, "main", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                h.run(["--task-file", "x", "--workspace", str(self.ws)])
        self.assertIn("unhandled:", self.log())
        self.assertIn("RuntimeError: boom", self.log())

    def test_a_systemexit_passes_through_unlogged(self):
        with unittest.mock.patch.object(h, "main", side_effect=SystemExit(2)):
            with self.assertRaises(SystemExit):
                h.run(["--task-file", "x", "--workspace", str(self.ws)])
        self.assertNotIn("unhandled:", self.log())

    def test_workspace_arg_is_read_in_both_spellings(self):
        self.assertEqual(h._workspace_arg(["--workspace", "/w"]), "/w")
        self.assertEqual(h._workspace_arg(["--workspace=/w2"]), "/w2")
        self.assertIsNone(h._workspace_arg(["--task-file", "t"]))

    def test_the_retry_pass_cli_prints_its_outcome(self):
        self.roster()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = h.main(["--retry-pass", "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["delivered"], [])

    def _run_with_stores(self, primary_blocked=True, fallback_blocked=False):
        """A real run that defers, with one or both marker stores obstructed by
        a file — kewei's reproduction, extended to the second store."""
        import contextlib
        import io
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}, "f" * 32: {"state": "live"}},
             "bindings": {"!room:x": [W, "f" * 32]}}))
        if primary_blocked:
            h.retry_dir(self.ws).write_text("a file where the dir should be")
        if fallback_blocked:
            h.retry_dirs(self.ws)[1].write_text("and one here too")
        t = self.task_file("task-1", channel_id="!room:x")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = h.main(["--task-file", t, "--workspace", str(self.ws)])
        return rc, err.getvalue()

    def test_an_obstructed_primary_store_still_parks_the_task_somewhere(self):
        """The settled answer means RECOVERABLE. With the primary store a file,
        the marker lands in the second store and exit 0 remains truthful."""
        rc, err = self._run_with_stores()
        self.assertEqual(rc, 0)
        self.assertTrue((h.retry_dirs(self.ws)[1] / "task-1").exists())
        self.assertNotIn("DEFERRED WITHOUT RETRY MARKER", err)
        self.assertTrue(h.parked(self.ws, "task-1"))

    def test_a_marker_in_the_second_store_is_redelivered_automatically(self):
        """kewei's asked regression: marker-write failure -> storage repaired ->
        the NEXT pass delivers it, with no restart and no core involvement."""
        self._run_with_stores()
        self.roster()          # the roster fault is repaired
        h.retry_dir(self.ws).unlink()   # so is the obstructed store
        out = h.retry_pass(str(self.ws))
        self.assertEqual(out["delivered"], ["task-1"])
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())
        self.assertFalse(h.parked(self.ws, "task-1"))

    def test_a_marker_in_the_second_store_survives_a_still_obstructed_primary(self):
        """The control on the case above: the pass reads the second store even
        while the first is still a file, so recovery needs no repair at all."""
        self._run_with_stores()
        self.roster()
        out = h.retry_pass(str(self.ws))
        self.assertEqual(out["delivered"], ["task-1"])
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_no_store_at_all_is_unsettled_rather_than_settled_or_fallback(self):
        """With nowhere durable to record ownership, 0 would settle work no pass
        can find and any other non-zero would hand it to the core."""
        rc, err = self._run_with_stores(fallback_blocked=True)
        self.assertEqual(rc, h.UNSETTLED)
        self.assertNotEqual(h.UNSETTLED, 0)
        self.assertIn("DEFERRED WITHOUT RETRY MARKER", err)
        self.assertIn(h.UNMARKED_NOTICE, err)
        self.assertIn("task-1", err)
        for d in h.retry_dirs(self.ws):
            self.assertIn(str(d), err)
        self.assertIn("keeps its claim", err)
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_a_deferral_that_keeps_its_marker_says_nothing_on_stderr(self):
        """The control: the notice names a real loss, not every deferral."""
        import contextlib
        import io
        err = io.StringIO()
        t = self.task_file("task-1", channel_id="!room:x")
        with contextlib.redirect_stderr(err):
            self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((h.retry_dir(self.ws) / "task-1").exists())
        self.assertFalse(h.retry_dirs(self.ws)[1].exists())
        self.assertNotIn("DEFERRED WITHOUT RETRY MARKER", err.getvalue())

    def test_a_delivered_task_leaves_no_marker_in_either_store(self):
        self.roster()
        for d in h.retry_dirs(self.ws):
            d.mkdir(parents=True, exist_ok=True)
            (d / "task-1").write_text("refused: earlier\n")
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertFalse(h.parked(self.ws, "task-1"))


class TestParkedQuestion(Base):
    """The Stop hook's exemption question. It must never read "I could not
    tell" as "nobody parked it" — that is what tells the core to answer a
    worker's task while it waits for a pass."""

    def test_a_marker_in_either_store_answers_parked(self):
        for d in h.retry_dirs(self.ws):
            d.mkdir(parents=True, exist_ok=True)
            (d / "task-1").write_text("x\n")
            self.assertEqual(h.main(["--workspace", str(self.ws), "--parked", "task-1"]), 0)
            (d / "task-1").unlink()

    def test_no_marker_anywhere_answers_not_parked(self):
        self.assertEqual(h.main(["--workspace", str(self.ws), "--parked", "task-1"]), 1)

    def test_a_store_obstructed_by_a_file_is_a_negative_not_an_unknown(self):
        """Nothing can be stored under a file, so this store provably holds no
        marker; calling it unknown would switch the Stop hook off for every task."""
        h.retry_dir(self.ws).write_text("a file where the dir should be")
        self.assertEqual(h.main(["--workspace", str(self.ws), "--parked", "task-1"]), 1)

    def test_an_unreadable_store_is_unknown_not_a_denial(self):
        import os
        d = h.retry_dir(self.ws)
        d.mkdir(parents=True)
        os.chmod(d, 0o000)
        self.addCleanup(os.chmod, d, 0o755)
        if os.access(d, os.R_OK):
            self.skipTest("the mode did not take effect (running as root?)")
        self.assertEqual(h.main(["--workspace", str(self.ws), "--parked", "task-1"]),
                         h.PARKED_UNKNOWN)
        self.assertGreater(h.PARKED_UNKNOWN, 1)

    def test_a_found_marker_answers_even_when_another_store_is_blind(self):
        """Unknown is only for a question that cannot be answered; a marker in a
        readable store is an answer whatever the other store is doing."""
        import os
        blind = h.retry_dir(self.ws)
        blind.mkdir(parents=True)
        os.chmod(blind, 0o000)
        self.addCleanup(os.chmod, blind, 0o755)
        d = h.retry_dirs(self.ws)[1]
        d.mkdir(parents=True, exist_ok=True)
        (d / "task-1").write_text("x\n")
        self.assertEqual(h.main(["--workspace", str(self.ws), "--parked", "task-1"]), 0)

    def test_the_parked_question_writes_no_run_line(self):
        """Asked once per task on every Stop; a log line each would bury the
        routing history the log exists for."""
        h.run(["--workspace", str(self.ws), "--parked", "task-1"])
        p = self.ws / "logs" / "pool-route-handler.log"
        self.assertNotIn("run returning", p.read_text() if p.exists() else "")


if __name__ == "__main__":
    unittest.main(verbosity=0)

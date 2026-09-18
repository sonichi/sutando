#!/usr/bin/env python3
"""Attribution is read from ASSIGNMENT truth, not inferred from completion
residue: the bridge stamps metadata.worker_id from the record the router wrote
when it handed the task out, so a result carries its author even when no
done-flag residue exists at the moment the drain reads it.

The failure this replaces: _worker_of() answers only from
state/workers/<recipient>/done/<tid>.{flag,pending}, which exists only because
the worker got far enough to lay it and survives only as long as nothing
reaps it. Attribution that depends on the completion path cannot describe a
result whose completion path was interrupted — and the whole point of
attribution is to be right about exactly those.

Fixtures are built through the real recorder (pool_attribution.record) rather
than by hand-spelling state/attribution/<tid>: packages/ag2-sparrow is a
standalone PyPI package that cannot import skills/worker-pool/ in production,
so _assigned_worker()'s path literal is the only place the convention is
re-stated. Building from the writer is what makes a future drift fail here
instead of silently returning "" forever (the 2026-09-16 "cores" vs "workers"
drift shipped every result with no worker_id and six tests stayed green,
because the fixture reimplemented the same wrong path).

Run: python3 tests/gateway-assignment-attribution.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "remote-gateway-bridge.py"
_POOL_SCRIPTS = _REPO / "skills" / "worker-pool" / "scripts"

sys.path.insert(0, str(_POOL_SCRIPTS))
import pool_attribution  # noqa: E402
import pool_delivery  # noqa: E402
import pool_roster  # noqa: E402

W1 = "02e4302f00844397bac09533fc398248"
W2 = "212e8040d38d48b5aadab0db295dc33a"


def _load():
    spec = importlib.util.spec_from_file_location("_rgb_assign", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rgb_assign"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Captured(Exception):
    """Stops _deliver_result_payload once the payload is built, so assertions
    never depend on delivery-status enum semantics."""


class AssignmentAttribution(unittest.TestCase):
    def setUp(self):
        self.mod = _load()
        self.workspace = tempfile.mkdtemp()
        self.mod._STATE = Path(self.workspace) / "state"
        self.logs = []
        self.mod._log = self.logs.append
        self.seen = {}

        class _Backend:
            def publish(_s, tid, payload):
                self.seen["payload"] = json.loads(payload.decode())
                raise _Captured()

        self.mod._delivery_core = lambda: type("C", (), {"backend": _Backend()})()

    def _assign(self, tid: str, worker: str):
        """Through the router's own recorder, never a hand-spelled path."""
        self.assertTrue(pool_attribution.record(self.workspace, tid, worker))

    def _residue(self, recipient: str, tid: str):
        path = pool_delivery.done_flag(Path(self.workspace), recipient, tid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def _sentinel(self, recipient: str, tid: str):
        """Through pool_delivery's own path function: the convention the bridge
        re-states is the one the pool writes, or this test cannot catch drift."""
        d = pool_delivery.deliveries_dir(Path(self.workspace), recipient)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}{pool_delivery.PENDING_SUFFIX}").write_text("")

    def _doc(self, tid: str) -> dict:
        self.seen.clear()
        with self.assertRaises(_Captured):
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        return self.seen["payload"]

    # --- the case the old path could not answer -------------------------

    def test_assignment_alone_attributes_with_no_residue(self):
        """THE REGRESSION GUARD. No done-flag at all — the state after a reap,
        or before the worker promotes .pending. Residue-only attribution
        returns "" here; assignment truth answers."""
        tid = "task-aa11bb22cc33dd44ee"
        self._assign(tid, W1)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})

    def test_assignment_wins_over_disagreeing_residue(self):
        """Residue is an inference; assignment is the routing decision itself.
        When they disagree the recorded decision is authoritative."""
        tid = "task-bb22cc33dd44ee55ff"
        self._assign(tid, W1)
        self._residue(W2, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})

    # --- the transition window must not regress -------------------------

    def test_residue_still_answers_when_unassigned(self):
        """A task routed before assignment records existed keeps working."""
        tid = "task-cc33dd44ee55ff6600"
        self._residue(W2, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W2})

    def test_residue_fallback_surfaces_the_anomaly(self):
        """A result whose author was never recorded at routing time is the
        thing the store exists to catch — it must not pass silently."""
        tid = "task-dd44ee55ff66001122"
        self._residue(W2, tid)
        self._doc(tid)
        self.assertTrue(
            any("no assignment record" in m for m in self.logs),
            f"expected an anomaly log, got {self.logs}",
        )

    def test_assigned_task_logs_nothing(self):
        """Control for the assertion above: the anomaly log fires on the
        anomaly, not on every result."""
        tid = "task-ee55ff6600112233"
        self._assign(tid, W1)
        self._doc(tid)
        self.assertEqual(
            [m for m in self.logs if "no assignment record" in m], [])

    def test_neither_source_means_no_stamp(self):
        """A core-handled result carries no worker metadata at all — absent,
        not an empty string a consumer might render."""
        doc = self._doc("task-ff66001122334455")
        self.assertNotIn("metadata", doc)

    # --- a delivered task with no attribution must not pass silently -----

    def test_delivered_without_record_refuses_and_says_so(self):
        """THE GUARD. A delivery sentinel proves this was a worker's task, so
        returning nothing silently would relay it as if the core produced it.

        This used to assert the payload was built WITHOUT metadata — i.e. that
        the result still went out, unattributed. That was the standing blocker,
        so the assertion is now that nothing is built at all: _doc() drives
        publish, and publish is never reached."""
        tid = "task-5566778899001122"
        self._sentinel(W1, tid)
        self.seen.clear()
        self.assertFalse(
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!"))
        self.assertNotIn("payload", self.seen,
                         "a refused result must not reach publish at all")
        self.assertTrue(
            any("refusing to stamp" in m for m in self.logs),
            f"expected a loud refusal, got {self.logs}",
        )

    def test_core_result_stays_quiet(self):
        """Control: no sentinel, no record, no residue is an ORDINARY core
        result. If this fired, every core reply would log an anomaly."""
        self._doc("task-6677889900112233")
        self.assertEqual(
            [m for m in self.logs if "refusing to stamp" in m], [])

    def test_a_recorded_delivery_does_not_warn(self):
        """Control: sentinel AND record present is the normal worker path."""
        tid = "task-7788990011223344"
        self._assign(tid, W1)
        self._sentinel(W1, tid)
        self.assertEqual(self._doc(tid)["metadata"], {"worker_id": W1})
        self.assertEqual(
            [m for m in self.logs if "refusing to stamp" in m], [])

    # --- fails closed ----------------------------------------------------

    def test_directory_is_refused(self):
        """A non-regular record is malformed; the recorder would refuse to
        write it, so the reader refuses to believe it."""
        tid = "task-0011223344556677"
        (Path(self.workspace) / "state" / "attribution" / tid).mkdir(parents=True)
        self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_non_worker_content_is_refused(self):
        """`core` is not a worker id. Recording one would make the bridge
        stamp a worker that never ran."""
        tid = "task-1122334455667788"
        d = Path(self.workspace) / "state" / "attribution"
        d.mkdir(parents=True, exist_ok=True)
        (d / tid).write_text("core")
        self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_unreadable_record_is_refused(self):
        """An OSError from the stat is NO READING, not "never assigned" — the
        distinction fail-closed depends on, so it is exercised, not asserted."""
        tid = "task-3344556677889900"
        self._assign(tid, W1)
        with mock.patch.object(self.mod.os, "lstat",
                               side_effect=PermissionError("denied")):
            self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_unreadable_content_is_refused(self):
        """Same for a record that stats fine and cannot be read."""
        tid = "task-4455667788990011"
        self._assign(tid, W1)
        with mock.patch.object(Path, "read_text",
                               side_effect=PermissionError("denied")):
            self.assertEqual(self.mod._assigned_worker(tid), "")

    def test_delivery_traversal_is_refused(self):
        self.assertEqual(self.mod._delivery_recipient("../../etc/passwd")[0], "")

    def test_unreadable_deliveries_root_is_refused(self):
        """An unreadable root is NO READING, not "never delivered" — the whole
        point of the discriminator is that those differ. Both yield "", so the
        LOG is what makes them differ; without it this test asserted the very
        conflation its docstring rejects."""
        tid = "task-8899001122334455"
        self._sentinel(W1, tid)
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("x")):
            self.assertEqual(self.mod._delivery_recipient(tid)[0], "")
        self.assertTrue(
            any("BLIND" in m for m in self.logs),
            f"an unreadable root must say so, got {self.logs}",
        )

    def test_never_delivered_is_silent_where_blind_is_loud(self):
        """The other half of the discriminator: a genuinely undelivered task
        must NOT log the blind anomaly, or the signal means nothing."""
        self.assertEqual(
            self.mod._delivery_recipient("task-8899001122334456")[0], "")
        self.assertEqual([m for m in self.logs if "BLIND" in m], [])

    def test_another_tasks_sentinel_is_not_ours(self):
        """A populated recipient dir holding somebody else's sentinel must not
        claim this task — both suffixes miss and the loop falls through."""
        self._sentinel(W1, "task-9900112233445566")
        self.assertEqual(self.mod._delivery_recipient("task-0011223344556677x")[0], "")

    def test_unreadable_sentinel_is_refused(self):
        tid = "task-1122334455667780"
        self._sentinel(W1, tid)
        with mock.patch.object(self.mod.os, "lstat",
                               side_effect=PermissionError("x")):
            self.assertEqual(self.mod._delivery_recipient(tid)[0], "")

    def test_non_regular_sentinel_is_refused(self):
        """A directory named like a sentinel is malformed; the writer would
        never make one, so the reader refuses to believe it."""
        tid = "task-2233445566778890"
        d = pool_delivery.deliveries_dir(Path(self.workspace), W1)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}{pool_delivery.PENDING_SUFFIX}").mkdir()
        self.assertEqual(self.mod._delivery_recipient(tid)[0], "")

    def test_accepted_stage_counts_too(self):
        """Positive control for the second suffix: a task already accepted is
        still evidence it was delivered."""
        tid = "task-3344556677889901"
        d = pool_delivery.deliveries_dir(Path(self.workspace), W1)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}{pool_delivery.ACCEPTED_SUFFIX}").write_text("")
        self.assertEqual(self.mod._delivery_recipient(tid)[0], W1)

    def test_legacy_accepted_suffix_counts_too(self):
        """Work accepted under the old sentinel name is still evidence of
        delivery; the pool's own matcher still recognises it."""
        tid = "task-3344556677889902"
        d = pool_delivery.deliveries_dir(Path(self.workspace), W1)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{tid}{pool_delivery.LEGACY_ACCEPTED_SUFFIX}").write_text("")
        self.assertEqual(self.mod._delivery_recipient(tid)[0], W1)

    def test_fan_out_to_two_recipients_claims_neither(self):
        """Two claimants is not one answer. Asserted against the GUARD too, so
        the multi-claimant arm cannot go quiet unnoticed."""
        tid = "task-3344556677889903"
        self._sentinel(W1, tid)
        self._sentinel(W2, tid)
        # The whole pair: an empty id alone is what zero claimants returns too,
        # so asserting [0] lets the ambiguous case pass as an ordinary core result.
        self.assertEqual(self.mod._delivery_recipient(tid), ("", True))

    def test_fan_out_is_refused_through_attribution(self):
        """The safety property, at the caller that decides whether to send: two
        sentinels and no assignment record must REFUSE, not read as core."""
        tid = "task-3344556677889904"
        self._sentinel(W1, tid)
        self._sentinel(W2, tid)
        self.assertEqual(self.mod._attribution(tid), ("", True))

    def test_zero_claimants_still_reads_as_core(self):
        """The control the refusal must not swallow: no sentinel at all is an
        ordinary core result, and stays (\"\", False)."""
        tid = "task-3344556677889905"
        self.assertEqual(self.mod._delivery_recipient(tid), ("", False))
        self.assertEqual(self.mod._attribution(tid), ("", False))

    # --- the core is a recipient, and that is not an anomaly -------------

    def test_core_sentinel_is_not_a_worker_claim(self):
        """BLOCKER 1. The router writes a sentinel for EVERY recipient but
        records attribution only for workers, so "core sentinel, no record" is
        the ordinary shape of a core-answered task — not the invariant
        violation the guard exists to report."""
        tid = "task-4455667788990011"
        self._sentinel(pool_roster.CORE, tid)
        self.assertEqual(self.mod._delivery_recipient(tid)[0], "")

    def test_core_routed_task_does_not_trip_the_guard(self):
        """The same case through the real caller: ordinary core traffic must
        not log a scary refusal, or every core reply would carry one."""
        tid = "task-4455667788990012"
        self._sentinel(pool_roster.CORE, tid)
        doc = self._doc(tid)
        self.assertNotIn("metadata", doc)
        self.assertEqual(
            [m for m in self.logs if "refusing to stamp" in m], [])

    def test_a_worker_is_still_claimed_beside_a_core_sentinel(self):
        """Control for the core filter: skipping core must not skip a real
        worker delivered the same task."""
        tid = "task-4455667788990013"
        self._sentinel(pool_roster.CORE, tid)
        self._sentinel(W1, tid)
        self.assertEqual(self.mod._delivery_recipient(tid)[0], W1)

    def test_a_stray_file_at_the_root_does_not_blind_the_guard(self):
        """BLOCKER 2. Reading THROUGH a non-directory raises NotADirectoryError,
        an OSError — treating that as a read failure turned the guard off for
        every task on the host the moment one .DS_Store appeared."""
        tid = "task-4455667788990014"
        self._sentinel(W1, tid)
        root = pool_delivery.deliveries_dir(Path(self.workspace), W1).parent
        (root / ".DS_Store").write_text("")
        self.assertEqual(self.mod._delivery_recipient(tid)[0], W1)
        self.assertEqual([m for m in self.logs if "BLIND" in m], [])

    def test_bridge_constants_cover_what_the_pool_writes(self):
        """THE DRIFT GUARD the fixtures alone could not give: the fixtures only
        ever build PENDING/ACCEPTED, so a suffix or recipient name the pool
        recognises and this module does not would pass every other test here."""
        self.assertEqual(self.mod._CORE_RECIPIENT, pool_roster.CORE)
        for suffix in (pool_delivery.PENDING_SUFFIX,
                       pool_delivery.ACCEPTED_SUFFIX,
                       pool_delivery.LEGACY_ACCEPTED_SUFFIX):
            self.assertIn(suffix, self.mod._DELIVERY_SUFFIXES)

    # --- the refusal is ENFORCED, not merely logged ----------------------

    def _counting_core(self):
        """A core that RECORDS publish/deliver instead of aborting, so a test
        can assert the calls did not happen. The class-level _Backend raises on
        publish, which cannot distinguish "skipped" from "reached"."""
        calls = {"publish": 0, "deliver": 0}
        mod = self.mod

        class _Res:
            # ATTEMPTED + CONFIRMED is the real happy path: the caller checks
            # TERMINAL/NOT_CLAIMED first, then res.outcome.
            status = mod.DrainStatus.ATTEMPTED
            outcome = mod.CoreDeliveryOutcome.CONFIRMED

        class _Backend:
            def publish(_s, tid, payload):
                calls["publish"] += 1
                return True

            def attempts(_s, tid):
                return 1

        class _Core:
            backend = _Backend()
            provider = object()
            worker = "test"

            def deliver_one(_s, tid, payload):
                calls["deliver"] += 1
                return _Res()

        self.mod._delivery_core = lambda: _Core()
        return calls

    def test_refused_result_is_neither_published_nor_delivered(self):
        """THE STANDING BLOCKER. A delivery sentinel with no record means a
        worker owned this task and nothing recorded who; publishing it relays a
        worker's reply as the core's own. The log alone never stopped it."""
        tid = "task-5566778899001133"
        self._sentinel(W1, tid)
        calls = self._counting_core()
        ok = self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        self.assertFalse(ok, "a withheld result must not report confirmed")
        self.assertEqual(calls, {"publish": 0, "deliver": 0},
                         f"refused result still reached the wire: {calls}")
        self.assertTrue(any("withholding" in m for m in self.logs),
                        f"expected the refusal to say it withheld, got {self.logs}")

    def test_ordinary_core_result_still_publishes_and_delivers(self):
        """THE CONTROL that keeps the guard honest: same call, no sentinel, so
        an ordinary core-routed result must still go out unchanged. Without
        this, refusing everything would pass the test above."""
        tid = "task-5566778899001134"
        calls = self._counting_core()
        ok = self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        self.assertTrue(ok)
        self.assertEqual(calls, {"publish": 1, "deliver": 1})

    def test_an_attributed_worker_result_still_publishes(self):
        """Second control: a worker result that IS recorded is not withheld —
        the refusal keys on missing attribution, not on being a worker's."""
        tid = "task-5566778899001135"
        self._assign(tid, W1)
        self._sentinel(W1, tid)
        calls = self._counting_core()
        self.assertTrue(self.mod._deliver_result_payload(tid, f"broker-{tid}", "x"))
        self.assertEqual(calls, {"publish": 1, "deliver": 1})

    def test_refused_result_with_a_file_is_quarantined(self):
        """With a result file in hand the refusal must terminate, not retry
        forever — same quarantine the outbox's own terminal arm uses."""
        tid = "task-5566778899001136"
        self._sentinel(W1, tid)
        self._counting_core()
        seen = {}
        self.mod._quarantine_undelivered = lambda rf, t, why: seen.update(
            {"file": rf, "tid": t, "why": why})
        rf = Path(self.workspace) / f"{tid}.txt"
        rf.write_text("done!")
        self.assertFalse(
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!",
                                             result_file=rf))
        self.assertEqual(seen.get("tid"), tid)
        self.assertIn("attribution refused", seen.get("why", ""))

    # --- an unreadable evidence store must WITHHOLD, not assume core --------

    def test_unreadable_root_withholds_at_the_caller(self):
        """THE FAIL-OPEN THIS CLOSES. A log is not a decision: the sender could
        not see it, so an unreadable deliveries tree published every result as
        an ordinary core result — the guard went dark exactly when its evidence
        store was broken."""
        tid = "task-6677889900112244"
        self._sentinel(W1, tid)
        calls = self._counting_core()
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("x")):
            ok = self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        self.assertFalse(ok)
        self.assertEqual(calls, {"publish": 0, "deliver": 0},
                         f"blind discriminator still reached the wire: {calls}")
        self.assertTrue(any("UNKNOWN" in m for m in self.logs), self.logs)

    def test_unreadable_sentinel_withholds_at_the_caller(self):
        """The per-entry half of the same hole: iterdir succeeds, the lstat
        beneath it does not."""
        tid = "task-6677889900112245"
        self._sentinel(W1, tid)
        calls = self._counting_core()
        with mock.patch.object(self.mod.os, "lstat",
                               side_effect=PermissionError("x")):
            ok = self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!")
        self.assertFalse(ok)
        self.assertEqual(calls, {"publish": 0, "deliver": 0})

    def test_a_host_with_no_deliveries_tree_is_not_blind(self):
        """THE CONTROL that bounds the blast radius. An ABSENT tree is a host
        with no worker pool — knowledge, not failure. If this withheld, a
        single-core install would stop delivering everything."""
        tid = "task-6677889900112246"
        self.assertFalse((Path(self.workspace) / "deliveries").exists())
        calls = self._counting_core()
        self.assertTrue(
            self.mod._deliver_result_payload(tid, f"broker-{tid}", "done!"))
        self.assertEqual(calls, {"publish": 1, "deliver": 1})
        self.assertEqual([m for m in self.logs if "UNKNOWN" in m], [])

    def test_blind_is_reported_separately_from_absent(self):
        """The pair at the discriminator itself: same "" recipient, different
        second element — which is the whole reason it is a pair."""
        tid = "task-6677889900112247"
        self._sentinel(W1, tid)
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("x")):
            self.assertEqual(self.mod._delivery_recipient(tid), ("", True))
        self.assertEqual(
            self.mod._delivery_recipient("task-6677889900112248"), ("", False))

    def test_attribution_reports_refusal_separately_from_no_worker(self):
        """The pair exists because "" alone cannot separate these two."""
        refused_tid = "task-5566778899001137"
        self._sentinel(W1, refused_tid)
        self.assertEqual(self.mod._attribution(refused_tid), ("", True))
        self.assertEqual(self.mod._attribution("task-5566778899001138"), ("", False))
        # the thin wrapper flattens both to "" — which is why senders must not use it
        self.assertEqual(self.mod._result_worker(refused_tid), "")

    def test_traversal_is_refused(self):
        self.assertEqual(self.mod._assigned_worker("../../etc/passwd"), "")

    def test_recorder_and_reader_agree_on_the_path(self):
        """The drift guard: the file the recorder writes is the file the
        reader reads. If either side moves, this fails."""
        tid = "task-2233445566778899"
        self._assign(tid, W1)
        self.assertTrue(
            pool_attribution.attribution_path(self.workspace, tid).is_file())
        self.assertEqual(self.mod._assigned_worker(tid), W1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

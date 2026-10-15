#!/usr/bin/env python3
"""Operator recovery: PARKED -> QUEUED must be atomic, idempotent, and must
never manufacture a second delivery.

Epoch assertions run against the SHIPPED key derivation, not a re-computation.
"""
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as um
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "ag2-sparrow"))

import outbox  # noqa: E402
import outbox_cli  # noqa: E402

import undelivered_quarantine as uq  # noqa: E402

ITEM = "task-abc"
OTHER = "task-def"


def _parked(root: Path, item_id: str = ITEM, attempts: int = 5) -> None:
    outbox._write_item(root, item_id, {"item_id": item_id, "attempts": attempts,
                                       "status": "QUEUED", "reason": None})
    outbox.park_item(root, item_id, "max-attempts")


class RequeueTransition(unittest.TestCase):
    def test_parked_to_queued_bumps_epoch_and_records_operator(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 0)
            r = outbox.requeue_item(root, ITEM, operator="alice", reason="relay 503")
            self.assertIs(r, outbox.RequeueOutcome.REQUEUED)
            rec = outbox.read_item(root, ITEM)
            self.assertEqual(rec["status"], "QUEUED")
            self.assertEqual(rec["resend_epoch"], 1)
            self.assertEqual(rec["requeued_by"], "alice")
            self.assertEqual(rec["requeue_reason"], "relay 503")
            self.assertIsNone(rec["reason"])

    def test_attempts_preserved_unless_reset_requested(self):
        """Opt-in per the operator brief: the default keeps the count."""
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root, attempts=5)
            outbox.requeue_item(root, ITEM)
            self.assertEqual(outbox.attempts_for(root, ITEM), 5)
            outbox.park_item(root, ITEM, "max-attempts")
            outbox.requeue_item(root, ITEM, reset_attempts=True)
            self.assertEqual(outbox.attempts_for(root, ITEM), 0)

    def test_repeated_requeue_is_idempotent(self):
        """The second call must not re-bump the epoch: a queued item is not a
        parked one, so there is nothing to recover."""
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.REQUEUED)
            for _ in range(3):
                self.assertIs(outbox.requeue_item(root, ITEM),
                              outbox.RequeueOutcome.NOT_PARKED)
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 1)

    def test_delivered_item_is_never_requeued(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            outbox.record_delivered(root, ITEM, provider="p", destination="d")
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.NOT_PARKED)
            self.assertEqual(outbox.item_status(root, ITEM), "DELIVERED")

    def test_absent_item_reports_absent(self):
        with TemporaryDirectory() as td:
            self.assertIs(outbox.requeue_item(Path(td), "nope"),
                          outbox.RequeueOutcome.ABSENT)


class ClaimSafety(unittest.TestCase):
    def test_a_live_claim_on_an_unparked_item_survives(self):
        """The no-concurrent-delivery guarantee. An item holding a live claim is
        by definition not PARKED, so requeue refuses BEFORE any force-release —
        it can never destroy a peer's in-flight delivery."""
        with TemporaryDirectory() as td:
            root = Path(td)
            outbox._write_item(root, ITEM, {"item_id": ITEM, "attempts": 1,
                                            "status": "CLAIMED", "reason": None})
            self.assertTrue(outbox.acquire_delivery_claim(root, ITEM, "drainer-1"))
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.NOT_PARKED)
            claim = outbox.read_delivery_claim(root, ITEM)
            self.assertIsNotNone(claim)
            self.assertEqual(claim.drainer_id, "drainer-1")

    def test_stale_claim_on_a_parked_item_is_cleared(self):
        """The residue case: parked, but a claim record outlived the attempt.
        Left in place it blocks every future drain, so requeue must clear it."""
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            outbox.acquire_delivery_claim(root, ITEM, "dead-drainer")
            self.assertIsNotNone(outbox.read_delivery_claim(root, ITEM))
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.REQUEUED)
            self.assertIsNone(outbox.read_delivery_claim(root, ITEM))
            self.assertTrue(outbox.acquire_delivery_claim(root, ITEM, "fresh"))


class CrashDuringTransition(unittest.TestCase):
    def test_crash_after_claim_release_leaves_it_parked_and_recoverable(self):
        """Ordering is the crash contract: claim first, status last. Interrupted
        between them, the item is still PARKED — never QUEUED-but-unclaimable —
        and a re-run completes the recovery."""
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            outbox.acquire_delivery_claim(root, ITEM, "dead-drainer")

            boom = RuntimeError("crash between release and status write")
            real_write = outbox._write_item

            def explode(r, i, d):
                if i == ITEM and d.get("status") == "QUEUED":
                    raise boom
                return real_write(r, i, d)

            outbox._write_item = explode
            try:
                with self.assertRaises(RuntimeError):
                    outbox.requeue_item(root, ITEM)
            finally:
                outbox._write_item = real_write

            self.assertEqual(outbox.item_status(root, ITEM), "PARKED")
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 0)
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.REQUEUED)
            self.assertEqual(outbox.item_status(root, ITEM), "QUEUED")
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 1)


    def test_crash_at_the_release_cannot_leave_queued_with_a_stale_claim(self):
        """Pins the ORDER, not just the outcome. Interrupt the claim release:
        with release first the status was never written (PARKED, recoverable);
        written first, the item would be QUEUED behind a claim no drain can
        take — deliverable-looking and permanently stuck."""
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            outbox.acquire_delivery_claim(root, ITEM, "dead-drainer")

            real_release = outbox._release_locked

            def explode(*a, **k):
                raise RuntimeError("crash during claim release")

            outbox._release_locked = explode
            try:
                with self.assertRaises(RuntimeError):
                    outbox.requeue_item(root, ITEM)
            finally:
                outbox._release_locked = real_release

            self.assertEqual(
                outbox.item_status(root, ITEM), "PARKED",
                "the status must not be written before the claim is released")
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 0)


class IdempotencyKeyChanges(unittest.TestCase):
    """#0 -> #1 asserted through the shipped derivation, not a re-computation."""

    def _keys_seen(self, root: Path, item_id: str) -> list:
        from ag2_sparrow.delivery_core import DesignAClaimBackend
        from ag2_sparrow.delivery_core.contract import (
            BackendCapabilities, DeliveryReceipt, ProviderCapabilities)
        from ag2_sparrow.delivery_core.core import DeliveryCore

        seen = []

        class Recorder:
            capabilities = ProviderCapabilities(reconcile_capable=False,
                                                idempotent_send=True)

            def deliver(self, item_id, payload, idempotency_key):
                seen.append(idempotency_key)
                return DeliveryReceipt(outcome=outbox.DeliveryOutcome.CONFIRMED)

        backend = DesignAClaimBackend(root)
        backend.publish(item_id, b"payload")
        DeliveryCore(backend, Recorder()).deliver_one(item_id, b"payload")
        return seen

    def test_key_moves_from_epoch_zero_to_one_after_requeue(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            first = self._keys_seen(root, ITEM)
            self.assertEqual(first, [f"{ITEM}#0"])
            outbox.park_item(root, ITEM, "max-attempts")
            self.assertIs(outbox.requeue_item(root, ITEM),
                          outbox.RequeueOutcome.REQUEUED)
            second = self._keys_seen(root, ITEM)
            self.assertEqual(second, [f"{ITEM}#1"],
                             "a requeued send must present a NEW key or the "
                             "provider dedupes it against the parked attempt")

    def test_backend_without_the_method_still_derives_epoch_zero(self):
        """Pre-existing behaviour for any backend that tracks no re-sends."""
        from ag2_sparrow.delivery_core.core import _resend_epoch
        self.assertEqual(_resend_epoch(object(), ITEM), 0)


class CliSurface(unittest.TestCase):
    def test_requeue_exit_codes_distinguish_recovered_from_nothing_to_do(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            argv = ["--root", str(root), "requeue", ITEM, "--operator", "bob"]
            self.assertEqual(outbox_cli.main(argv), 0)
            self.assertEqual(outbox_cli.main(argv), 3)      # idempotent re-run
            self.assertEqual(outbox_cli.main(["--root", str(root),
                                              "requeue", "ghost"]), 2)

    def test_list_filters_by_status(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root, ITEM)
            outbox.record_delivered(root, OTHER)
            parked = outbox.list_items(root, "PARKED")
            self.assertEqual([r["item_id"] for r in parked], [ITEM])
            self.assertEqual(len(outbox.list_items(root)), 2)

    def test_inspect_reports_the_claim_holder(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            outbox.acquire_delivery_claim(root, ITEM, "drainer-9")
            rec = outbox.read_item(root, ITEM)
            self.assertEqual(rec["status"], "PARKED")
            claim = outbox.read_delivery_claim(root, ITEM)
            self.assertEqual(claim.drainer_id, "drainer-9")


class BodyRestoredNotJustTheRecord(unittest.TestCase):
    """The record is half the recovery. The BODY is a result file the terminal
    path moved into results/undelivered/, and the drain's scan is a
    non-recursive glob over results/ — so a requeue that only flips the record
    delivers nothing, silently, which is the failure this PR exists to fix."""

    def _quarantined(self, td, root_inside_results=False):
        results = Path(td) / "results"
        results.mkdir()
        root = (results / ".outbox-test") if root_inside_results else Path(td) / "ob"
        _parked(root)
        (results / f"{ITEM}.txt").write_text("the reply", encoding="utf-8")
        uq.place(results / f"{ITEM}.txt", results, f"{ITEM}", when=1700000000)
        return root, results

    def test_the_DEFAULT_requeue_restores_the_body(self):
        """The common path must not be the broken one. `--root` is required and
        every lane puts the outbox at RESULTS_DIR/.outbox-*, so root.parent IS
        the results dir — no flag needed to get a complete recovery."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            self.assertEqual(outbox_cli.main(
                ["--root", str(root), "requeue", ITEM]), 0)
            self.assertTrue((results / f"{ITEM}.txt").exists(),
                            "requeue with no flag must still restore the body")

    def test_a_failed_restore_is_recoverable_by_re_running(self):
        """Interrupt exactly BETWEEN the two commits, then retry.

        `requeue_item` commits PARKED -> QUEUED and `restore()` moves the body;
        they are separate commits. Gating the restore on the REQUEUED transition
        meant a restore that raised left the record QUEUED with the body still
        quarantined, and every retry answered `not-parked` and skipped the
        restore — the user's result stranded outside every drain, permanently,
        while the operator was told the recovery had happened.
        """
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            live = results / f"{ITEM}.txt"

            real = uq.restore
            outbox_cli.undelivered_quarantine.restore = \
                lambda *a, **k: (_ for _ in ()).throw(OSError("simulated rename failure"))
            try:
                with self.assertRaises(OSError):
                    outbox_cli.main(["--root", str(root), "requeue", ITEM])
            finally:
                outbox_cli.undelivered_quarantine.restore = real

            # the record moved, the body did not: the half-committed state
            self.assertEqual((outbox.read_item(root, ITEM) or {}).get("status"), "QUEUED")
            self.assertFalse(live.exists())
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 1)

            # the retry must COMPLETE the recovery, not refuse it
            rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            self.assertEqual(rc, 0, "a retry that restored the body did work, so not 3")
            self.assertTrue(live.exists(), "the body must be back in the drain's view")
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 0)

    def _no_primitive(self, links=True):
        import unittest.mock as um
        m = outbox_cli.undelivered_quarantine
        ctx = [um.patch.object(m, "_RENAME", None), um.patch.object(m, "RENAME_PRIMITIVE", "none")]
        if not links:
            ctx.append(um.patch("os.link", side_effect=PermissionError(1, "no hard links")))
        import contextlib
        stack = contextlib.ExitStack()
        for c in ctx:
            stack.enter_context(c)
        return stack

    def test_without_a_no_replace_rename_requeue_still_restores_the_body(self):
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            with self._no_primitive():
                rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            self.assertEqual(rc, 0)
            self.assertEqual((results / f"{ITEM}.txt").read_text(encoding="utf-8"), "the reply")
            self.assertEqual(outbox.item_status(root, ITEM), "QUEUED")

    def test_a_body_that_cannot_be_restored_fails_loudly_and_stays_queued(self):
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            out = []
            real = outbox_cli._emit
            outbox_cli._emit = lambda rec, as_json: out.append(dict(rec))
            try:
                with self._no_primitive(links=False):
                    rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            finally:
                outbox_cli._emit = real
            self.assertEqual(rc, 4, "a requeue that left the body quarantined must not exit 0")
            self.assertEqual(outbox.item_status(root, ITEM), "QUEUED",
                             "parking again would terminally refuse a reply published under this id")
            self.assertEqual(out[-1]["result"], "requeued")
            self.assertIn("could not be moved back", out[-1]["error"])
            self.assertIn("stays queued", out[-1]["error"])
            self.assertFalse((results / f"{ITEM}.txt").exists())
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 1, "the body must stay intact")

    def test_an_unrestorable_body_on_an_already_queued_record_is_left_queued(self):
        """Exit 4 names the failure; the record is not parked by the CLI,
        whichever run committed the transition."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            outbox.requeue_item(root, ITEM)                  # the record half committed earlier
            with self._no_primitive(links=False):
                rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            self.assertEqual(rc, 4)
            self.assertEqual(outbox.item_status(root, ITEM), "QUEUED")

    def test_a_peer_requeue_after_this_ones_lock_is_never_undone(self):
        """A peer parks and requeues to a new epoch right after this call's
        transition released the item lock; the rollback must leave the peer's cycle."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            real_lock, fired = outbox._item_lock, []

            @contextlib.contextmanager
            def lock_then_peer(*a, **k):
                with real_lock(*a, **k):
                    yield
                if not fired:
                    fired.append(1)
                    outbox.park_item(root, ITEM, "peer parked")
                    outbox.requeue_item(root, ITEM, operator="peer")

            out = []
            with self._no_primitive(links=False), \
                    um.patch.object(outbox, "_item_lock", lock_then_peer), \
                    um.patch.object(outbox_cli, "_emit", lambda rec, as_json: out.append(dict(rec))):
                rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            rec = outbox.read_item(root, ITEM) or {}
            self.assertEqual((rec.get("status"), rec.get("resend_epoch"), rec.get("requeued_by")),
                             ("QUEUED", 2, "peer"), "the peer's requeue must stand")
            self.assertEqual(rc, 4)
            self.assertEqual(out[-1]["resend_epoch"], 1, "the epoch this call wrote, not the peer's")

    def test_a_delivered_record_never_gets_its_body_back(self):
        """A copy left listed in undelivered/ (its aside rename failed) is not
        owed again once the record is DELIVERED: a re-run restores nothing."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            outbox.record_delivered(root, ITEM)
            rc = outbox_cli.main(["--root", str(root), "requeue", ITEM])
            self.assertEqual(rc, 3)
            self.assertFalse((results / f"{ITEM}.txt").exists(), "a delivered body went live again")
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 1)


    def test_a_retry_with_nothing_to_do_still_reports_nothing_to_do(self):
        """The exit code must not become 0 for every already-queued item, or the
        idempotent re-run loses the distinction the code exists to carry."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            self.assertEqual(outbox_cli.main(["--root", str(root), "requeue", ITEM]), 0)
            self.assertEqual(outbox_cli.main(["--root", str(root), "requeue", ITEM]), 3,
                             "second run restored nothing and moved nothing")

    def test_a_named_instance_recovers_with_both_ids(self):
        """The record and the body are filed under DIFFERENT ids on a named
        instance, so one argument cannot address both.

        `_delivery_core` publishes under the BROKER id while the result file —
        and therefore its quarantined copy — carries the instance-qualified
        LOCAL id (`task-<inst>~<broker>`). Passing the local id finds no record
        (`absent`); passing the broker id requeues the record but restores
        nothing. Recovery has to carry both.
        """
        with TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            root = results / ".outbox-dev"
            broker_id, local_id = "task-abc", "task-dev~task-abc"
            _parked(root, broker_id)
            (results / f"{local_id}.txt").write_text("the reply", encoding="utf-8")
            uq.place(results / f"{local_id}.txt", results, f"{local_id}", when=1700000000)

            # the local id addresses no record
            self.assertEqual(
                outbox_cli.main(["--root", str(root), "requeue", local_id]), 2,
                "the record is keyed by the broker id, so the local id is absent")

            # the broker id alone requeues the record and restores nothing
            self.assertEqual(
                outbox_cli.main(["--root", str(root), "requeue", broker_id]), 0)
            self.assertFalse((results / f"{local_id}.txt").exists(),
                             "the broker id cannot name the local body file")
            self.assertEqual(len(uq.find_quarantined(results, local_id)), 1)

            # both ids together complete the recovery
            self.assertEqual(outbox_cli.main(
                ["--root", str(root), "requeue", broker_id,
                 "--body-id", local_id]), 0)
            self.assertTrue((results / f"{local_id}.txt").exists(),
                            "--body-id must restore the body filed under the local id")
            self.assertEqual(len(uq.find_quarantined(results, local_id)), 0)

    def test_body_id_defaults_to_the_record_id(self):
        """The primary instance has one id for both; the flag must stay optional
        or every unnamed lane's recovery would need a redundant argument."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td, root_inside_results=True)
            self.assertEqual(outbox_cli.main(
                ["--root", str(root), "requeue", ITEM]), 0)
            self.assertTrue((results / f"{ITEM}.txt").exists())

    def test_outcome_distinguishes_absent_from_refused(self):
        """`None` for both left the operator unable to tell 'the body is gone'
        from 'a newer reply is already queued'."""
        with TemporaryDirectory() as td:
            results = Path(td) / "results"; results.mkdir()
            self.assertEqual(uq.restore(results, ITEM)[0],
                             uq.RestoreOutcome.NOTHING_QUARANTINED)
            (results / f"{ITEM}.txt").write_text("old", encoding="utf-8")
            uq.place(results / f"{ITEM}.txt", results, f"{ITEM}")
            (results / f"{ITEM}.txt").write_text("newer", encoding="utf-8")
            self.assertEqual(uq.restore(results, ITEM)[0],
                             uq.RestoreOutcome.LIVE_RESULT_PRESENT)

    def test_two_quarantines_in_one_second_do_not_collide(self):
        """Whole seconds silently overwrote; the incident had five attempts in
        six seconds, and recovery needs the set to be complete."""
        with TemporaryDirectory() as td:
            results = Path(td) / "results"; results.mkdir()
            for body in ("first", "second"):
                (results / f"{ITEM}.txt").write_text(body, encoding="utf-8")
                uq.place(results / f"{ITEM}.txt", results, f"{ITEM}")
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 2)

    def test_requeue_with_results_dir_returns_the_body_to_the_drain(self):
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td)
            self.assertEqual(outbox_cli.main(
                ["--root", str(root), "requeue", ITEM,
                 "--results-dir", str(results)]), 0)
            restored = results / f"{ITEM}.txt"
            self.assertTrue(restored.exists(),
                            "requeue must return the body the drain scans for")
            self.assertEqual(restored.read_text(encoding="utf-8"), "the reply")
            self.assertEqual(uq.find_quarantined(results, ITEM), [])

    def test_a_newer_live_result_is_never_overwritten(self):
        """A reply already waiting to go is the one the user should get."""
        with TemporaryDirectory() as td:
            root, results = self._quarantined(td)
            (results / f"{ITEM}.txt").write_text("newer", encoding="utf-8")
            outbox_cli.main(["--root", str(root), "requeue", ITEM,
                             "--results-dir", str(results)])
            self.assertEqual(
                (results / f"{ITEM}.txt").read_text(encoding="utf-8"), "newer")

    def test_the_drains_glob_cannot_see_the_quarantine(self):
        """Pins WHY the restore is needed: the scan is non-recursive."""
        with TemporaryDirectory() as td:
            _root, results = self._quarantined(td)
            self.assertEqual(sorted(results.glob("task-*.txt")), [])
            self.assertEqual(len(uq.find_quarantined(results, ITEM)), 1)

    def test_quarantine_naming_has_one_owner(self):
        """The bridge must not spell the quarantined name itself; two copies of
        a filename format is how the two directions stop agreeing."""
        bridge = (ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow"
                  / "remote_gateway_bridge.py").read_text(encoding="utf-8")
        self.assertNotIn('f"{rfile.stem}-{int(time.time())}.txt"', bridge)
        # Every quarantine goes through the lifecycle owner, which names files
        # only through undelivered_quarantine.place().
        self.assertIn("disposal.quarantine_current(", bridge)
        self.assertIn("disposal.quarantine_generation(", bridge)


class RequeueNeverChangesThePayload(unittest.TestCase):
    """A requeue sends the stored body; a later publish of the id is refused."""

    def test_the_stored_payload_survives_a_requeue_and_a_later_publish(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"))
        from ag2_sparrow.delivery_core import DesignAClaimBackend
        with TemporaryDirectory() as td:
            root = Path(td) / "ob"
            _parked(root)
            outbox._write_item(root, ITEM, dict(outbox.read_item(root, ITEM), payload="A"))
            outbox.requeue_item(root, ITEM)
            self.assertFalse(DesignAClaimBackend(root).publish(ITEM, b"B"))
            self.assertEqual(outbox.read_item(root, ITEM)["payload"], "A")


class DeliveredBodyDiffers(unittest.TestCase):
    """The owner rule: a live reply at a delivered id is sent only if its body is
    the one the delivered record stores."""

    def _rec(self, td, **fields):
        root = Path(td) / "ob"
        outbox._write_item(root, ITEM, dict(fields, item_id=ITEM))
        return root

    def test_cases(self):
        env = json.dumps({"id": ITEM, "body": "A"})
        other = json.dumps({"id": ITEM, "body": "B"})
        dig = outbox.source_digest

        def proof(source, payload=env):
            return outbox.source_proof_fields(dig(source), payload, "p1")
        for name, fields, live, differs in (
                ("no record", None, "C", False),
                ("queued", {"status": "QUEUED", "payload": env, **proof("A")}, "C", False),
                ("delivered, same source", {"status": "DELIVERED", "payload": env,
                                            **proof("[dm-only]\nA")}, "[dm-only]\nA", False),
                ("delivered, other source, same wire body", {"status": "DELIVERED", "payload": env,
                                                             **proof("A")}, "[dm-only]\nA", True),
                ("delivered, a proof written for another payload (stale), unchanged body",
                 {"status": "DELIVERED", "payload": env, **proof("B", other)}, "A", False),
                ("delivered, a proof with no payload binding", {"status": "DELIVERED", "payload": env,
                                                               "source_ready_sha256": dig("B")}, "A", False),
                ("legacy delivered, identical unmarked source", {"status": "DELIVERED", "payload": env}, "A", False),
                ("legacy delivered, marked source", {"status": "DELIVERED", "payload": env}, "[dm-only]\nA", True),
                ("legacy delivered, other source", {"status": "DELIVERED", "payload": env}, "C", True),
                ("legacy delivered, no envelope stored", {"status": "DELIVERED"}, "A", True),
                ("legacy delivered, unreadable envelope", {"status": "DELIVERED", "payload": "{"}, "A", True),
                ("legacy delivered, envelope not an object", {"status": "DELIVERED", "payload": "[]"}, "A", True),
                ("delivered, earlier source_sha256 only, identical body",
                 {"status": "DELIVERED", "payload": env, "source_sha256": dig("other")}, "A", False),
                ("delivered, earlier source_sha256 only, marked body",
                 {"status": "DELIVERED", "payload": env, "source_sha256": dig("[dm-only]\nA")}, "[dm-only]\nA", True),
                ("delivered, nothing readable live", {"status": "DELIVERED", "payload": env,
                                                      **proof("A")}, None, True)):
            with self.subTest(case=name), TemporaryDirectory() as td:
                root = self._rec(td, **fields) if fields is not None else Path(td) / "ob"
                self.assertIs(outbox.delivered_body_differs(root, ITEM, live), differs)

    def test_only_this_writers_stamp_with_a_matching_binding_is_trusted(self):
        env = json.dumps({"id": ITEM, "body": "A"})
        stamped = dict(outbox.source_proof_fields(outbox.source_digest("A"), env, "p1"),
                       payload=env, published_at=7.0)
        self.assertEqual(outbox.source_proof(stamped), outbox.source_digest("A"))
        for name, change in (("no stamp", {"proof_version": None}),
                             ("another stamp", {"proof_version": 2}),
                             ("payload rewritten by another writer", {"payload": env + " "}),
                             ("another publication", {"publication_id": "p2"}),
                             ("no publication id", {"publication_id": None}),
                             ("payload-only binding of an earlier head",
                              {"source_payload_sha256": hashlib.sha256(env.encode()).hexdigest()})):
            with self.subTest(case=name):
                self.assertIsNone(outbox.source_proof(dict(stamped, **change)))
        self.assertIsNone(outbox.source_proof(None))

    def test_invariant_publication_identity_is_unique_and_written_with_its_proof(self):
        """Two publications in the same millisecond never share an identity, and a
        requeue gives the record a new one, re-binding only a proof still trusted."""
        ids = {outbox.new_publication_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)
        with TemporaryDirectory() as td:
            root = Path(td) / "ob"
            env = json.dumps({"id": ITEM, "body": "A"})
            outbox._write_item(root, ITEM, {"item_id": ITEM, "status": "PARKED", "payload": env,
                                            "published_at": 7.0,
                                            **outbox.source_proof_fields(outbox.source_digest("A"), env, "p1")})
            outbox.requeue_item(root, ITEM)
            rec = outbox.read_item(root, ITEM)
            self.assertNotEqual(rec["publication_id"], "p1")
            self.assertEqual(outbox.source_proof(rec), outbox.source_digest("A"))
            outbox._write_item(root, ITEM, dict(rec, status="PARKED", payload=env + " "))
            outbox.requeue_item(root, ITEM)
            self.assertIsNone(outbox.source_proof(outbox.read_item(root, ITEM)), "a stale proof is never re-bound")

    def test_the_source_digest_is_of_the_ready_body(self):
        self.assertEqual(outbox.source_digest("A"), hashlib.sha256(b"A").hexdigest())


class CliRenderingAndErrorPaths(unittest.TestCase):
    """The CLI's own output and refusal paths. Calling outbox directly, as the
    other tests do, leaves every line of `_emit` and both readers unrun."""

    def _capture(self, argv):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = outbox_cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_list_empty_names_the_root_it_read(self):
        """An absent root and an empty one both list nothing, so the count
        alone cannot tell an operator which they hit (sonichi on #3853)."""
        with TemporaryDirectory() as td:
            rc, out, _ = self._capture(["--root", td, "list"])
            self.assertEqual(rc, 0)
            self.assertIn("(no items", out)
            self.assertIn(td, out, "the empty listing must name the root it read")
            absent = str(Path(td) / "nope")
            rc2, out2, _ = self._capture(["--root", absent, "list"])
            self.assertEqual(rc2, 0)
            self.assertIn(absent, out2)
            self.assertNotEqual(out, out2,
                                "absent and empty roots must be distinguishable")

    def test_list_renders_status_attempts_epoch_and_reason(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            rc, out, _ = self._capture(["--root", td, "list", "--status", "PARKED"])
            self.assertEqual(rc, 0)
            self.assertIn("PARKED", out)
            self.assertIn("attempts=5", out)
            self.assertIn("epoch=0", out)
            self.assertIn("max-attempts", out)

    def test_json_output_is_machine_readable(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            rc, out, _ = self._capture(["--root", td, "--json", "list"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)[0]["status"], "PARKED")

    def test_inspect_reports_the_claim_and_exits_2_when_absent(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            outbox.acquire_delivery_claim(root, ITEM, "drainer-9")
            rc, out, _ = self._capture(["--root", td, "inspect", ITEM])
            self.assertEqual(rc, 0)
            self.assertIn("drainer-9", out)
            rc, _, err = self._capture(["--root", td, "inspect", "ghost"])
            self.assertEqual(rc, 2)
            self.assertIn("no such item", err)

    def test_inspect_json_reports_a_null_claim_when_free(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            rc, out, _ = self._capture(["--root", td, "--json", "inspect", ITEM])
            self.assertEqual(rc, 0)
            self.assertIsNone(json.loads(out)["claim"])

    def test_operator_falls_back_when_the_login_name_is_unavailable(self):
        """A container with no passwd entry must still record something."""
        import getpass
        real = getpass.getuser
        getpass.getuser = lambda: (_ for _ in ()).throw(KeyError("no passwd"))
        try:
            self.assertEqual(outbox_cli._login_name(), None)
            self.assertTrue(outbox_cli._default_operator())
        finally:
            getpass.getuser = real


class OutboxReaderEdges(unittest.TestCase):
    """Malformed and absent state must degrade, never raise: these readers run
    on an operator's machine against a store a crashed writer may have left."""

    def test_absent_item_and_absent_store(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            self.assertIsNone(outbox.read_item(root, "ghost"))
            self.assertEqual(outbox.list_items(root), [])

    def test_a_corrupt_record_is_skipped_not_fatal(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            (outbox._items_dir(root) / "torn.json").write_text("{not json",
                                                              encoding="utf-8")
            got = outbox.list_items(root)
            self.assertEqual([r["item_id"] for r in got], [ITEM])

    def test_a_non_integer_epoch_reads_as_zero(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            outbox._write_item(root, ITEM, {"item_id": ITEM, "status": "QUEUED",
                                            "resend_epoch": "not-a-number"})
            self.assertEqual(outbox.resend_epoch_for(root, ITEM), 0)


class Delegation(unittest.TestCase):
    """`sutando outbox` must hand off, never re-implement outbox policy."""

    def test_sutando_outbox_delegates_verbatim(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sutando_runtime", ROOT / "src" / "runtime-cli" / "sutando-runtime.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with TemporaryDirectory() as td:
            root = Path(td)
            _parked(root)
            self.assertEqual(
                mod.main(["outbox", "--root", str(root), "requeue", ITEM]), 0)
            self.assertEqual(outbox.item_status(root, ITEM), "QUEUED")

    def test_cli_module_holds_no_transition_logic(self):
        """Policy lives in outbox.py. If the CLI ever writes a status itself,
        the two surfaces can disagree — which is the defect, not the symptom."""
        import re
        src = (ROOT / "src" / "outbox_cli.py").read_text(encoding="utf-8")
        private = sorted(set(re.findall(r"\boutbox\.(_\w+)", src)))
        self.assertEqual(private, [],
                         f"outbox_cli.py reaches into outbox internals: {private}")
        self.assertNotIn("_write_item", src)


class EntryPointImportsInThePackage(unittest.TestCase):
    """The console script imports ONLY ag2_sparrow.outbox_cli, so it must work
    with nothing else loaded. In-process this is unprovable: the bridge does
    sys.path.insert(0, src), so anything importing it first masks a flat-import
    failure. Hence a subprocess with only the package on PYTHONPATH.
    """

    def _run(self, code: str, pkg_parent: str):
        import subprocess
        env = dict(os.environ, PYTHONPATH=pkg_parent, PYTHONDONTWRITEBYTECODE="1")
        return subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, cwd=tempfile.gettempdir())

    def test_console_script_entry_imports_alone(self):
        import shutil
        with TemporaryDirectory() as td:
            shutil.copytree(ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow",
                            Path(td) / "ag2_sparrow")
            r = self._run("from ag2_sparrow.outbox_cli import main", td)
            self.assertEqual(r.returncode, 0,
                             f"`ag2-sparrow-outbox` cannot start:\n{r.stderr}")

    def test_flat_import_from_src_still_works(self):
        """The same file is src-canonical, where the names are top-level."""
        r = self._run("import outbox_cli", str(ROOT / "src"))
        self.assertEqual(r.returncode, 0, r.stderr)


class VendoredCopyInSync(unittest.TestCase):
    def test_package_copy_matches_src(self):
        a = (ROOT / "src" / "outbox_cli.py").read_text(encoding="utf-8")
        b = (ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow"
             / "outbox_cli.py").read_text(encoding="utf-8")
        self.assertIn(a.strip(), b, "run tools/sync_from_src.py")

    def test_entry_point_is_separate_not_a_dispatcher(self):
        """remote_gateway_bridge.main() reads no argv, so every invocation today
        starts the bridge; a dispatcher would change one of them."""
        toml = (ROOT / "packages" / "ag2-sparrow" / "pyproject.toml").read_text()
        self.assertIn('ag2-sparrow = "ag2_sparrow.remote_gateway_bridge:main"', toml)
        self.assertIn('ag2-sparrow-outbox = "ag2_sparrow.outbox_cli:main"', toml)


if __name__ == "__main__":
    unittest.main(verbosity=2)

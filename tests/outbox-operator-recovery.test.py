#!/usr/bin/env python3
"""Operator recovery: PARKED -> QUEUED must be atomic, idempotent, and must
never manufacture a second delivery.

Epoch assertions run against the SHIPPED key derivation, not a re-computation.
"""
import json
import os
import sys
import tempfile
import unittest
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
        uq.quarantine(results / f"{ITEM}.txt", results, when=1700000000)
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

    def test_outcome_distinguishes_absent_from_refused(self):
        """`None` for both left the operator unable to tell 'the body is gone'
        from 'a newer reply is already queued'."""
        with TemporaryDirectory() as td:
            results = Path(td) / "results"; results.mkdir()
            self.assertEqual(uq.restore(results, ITEM)[0],
                             uq.RestoreOutcome.NOTHING_QUARANTINED)
            (results / f"{ITEM}.txt").write_text("old", encoding="utf-8")
            uq.quarantine(results / f"{ITEM}.txt", results)
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
                uq.quarantine(results / f"{ITEM}.txt", results)
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
        self.assertIn("undelivered_quarantine.quarantine(", bridge)


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

    def test_list_empty_says_so_rather_than_printing_nothing(self):
        with TemporaryDirectory() as td:
            rc, out, _ = self._capture(["--root", td, "list"])
            self.assertEqual(rc, 0)
            self.assertIn("(no items)", out)

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

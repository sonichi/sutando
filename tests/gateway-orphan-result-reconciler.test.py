#!/usr/bin/env python3
"""Orphan-result reconciler (sonichi/sutando#3009): results outside the
in-flight ledger get exactly one of three dispositions — and the duplicate
fixture is a REAL double-write pair, because the sweep's failure mode is
posting agent narration into the room, not losing a reply.
Run: python3 tests/gateway-orphan-result-reconciler.test.py"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

from ag2_sparrow import remote_gateway_bridge as gw  # noqa: E402

TID = "task-00000000000000000a"
OLD = 3600.0            # older than every grace in play


def _age(p: Path, seconds: float) -> None:
    ts = time.time() - seconds
    import os
    os.utime(p, (ts, ts))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orphan-sweep-")
        root = Path(self.tmp.name)
        self._saved = {n: getattr(gw, n) for n in (
            "TASKS_DIR", "RESULTS_DIR", "ARCHIVE_RESULTS_DIR",
            "UNDELIVERABLE_RESULTS_DIR", "_req", "_delivery_tid", "_log",
            "_last_orphan_sweep")}
        gw.TASKS_DIR = root / "tasks"
        gw.RESULTS_DIR = root / "results"
        gw.ARCHIVE_RESULTS_DIR = gw.RESULTS_DIR / "archive"
        gw.UNDELIVERABLE_RESULTS_DIR = gw.RESULTS_DIR / "undelivered"
        for d in (gw.TASKS_DIR, gw.RESULTS_DIR):
            d.mkdir(parents=True)
        self.posted = []
        gw._req = lambda m, path, payload=None: self.posted.append((m, path, payload))
        gw._delivery_tid = lambda tid: tid
        self.logs = []
        gw._log = lambda m: self.logs.append(str(m))
        gw._last_orphan_sweep = 0.0
        gw._orphan_quarantine_logged.clear()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(gw, n, v)
        self.tmp.cleanup()

    def _result(self, body="the reply", age=OLD) -> Path:
        f = gw.RESULTS_DIR / f"{TID}.txt"
        f.write_text(body)
        _age(f, age)
        return f

    def _sweep(self):
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())


class DoubleWrite(_Base):
    def test_real_double_write_pair_moves_aside_never_delivers(self):
        # REAL fixture: first result delivered + archived by the normal path's
        # naming, then the same session writes a completion note 13s later.
        gw.ARCHIVE_RESULTS_DIR.mkdir(parents=True)
        (gw.ARCHIVE_RESULTS_DIR / f"{TID}-1786940000.txt").write_text("the reply")
        self._result("Replied in room ww — result was picked up and archived.")
        self._sweep()
        self.assertEqual(self.posted, [], "a duplicate must NEVER be re-delivered")
        moved = list(gw.ARCHIVE_RESULTS_DIR.glob(f"{TID}-*-late-duplicate.txt"))
        self.assertEqual(len(moved), 1, "duplicate moved aside into archive")
        self.assertFalse((gw.RESULTS_DIR / f"{TID}.txt").exists())


class GenuinelyUndelivered(_Base):
    def _archived_task(self, age=OLD):
        d = gw.TASKS_DIR / "archive"
        d.mkdir(parents=True, exist_ok=True)
        t = d / f"{TID}.txt"
        t.write_text(f"id: {TID}\ntask: hi\nsource: ag2space\n")
        _age(t, age)
        return t

    def test_labeled_redelivery_then_archive(self):
        self._archived_task()
        self._result("the reply")
        self._sweep()
        self.assertEqual(len(self.posted), 1)
        _m, path, payload = self.posted[0]
        self.assertEqual(path, "/v1/results")
        self.assertTrue(payload["body"].startswith("(recovered result"),
                        "re-delivery must carry the recovery label")
        self.assertIn("the reply", payload["body"])
        self.assertFalse((gw.RESULTS_DIR / f"{TID}.txt").exists(),
                         "delivered result archived out of results/")

    def test_lease_gone_quarantines(self):
        self._archived_task()
        self._result()

        def gone(m, path, payload=None):
            raise urllib.error.HTTPError(path, 410, "gone", {}, None)
        gw._req = gone
        self._sweep()
        q = list(gw.UNDELIVERABLE_RESULTS_DIR.glob(f"{TID}.lease-gone.*"))
        self.assertEqual(len(q), 1, "permanent 4xx quarantines, never retries forever")

    def test_network_error_leaves_for_next_sweep(self):
        self._archived_task()
        self._result()

        def down(m, path, payload=None):
            raise urllib.error.URLError("down")
        gw._req = down
        self._sweep()
        self.assertTrue((gw.RESULTS_DIR / f"{TID}.txt").exists(),
                        "transient failure retries next sweep")


class NoTask(_Base):
    def test_quarantine_and_log_once(self):
        self._result()
        self._sweep()
        self.assertEqual(len(list(gw.UNDELIVERABLE_RESULTS_DIR.glob(f"{TID}.no-task.*"))), 1)
        first = sum("no task file" in l for l in self.logs)
        self.assertEqual(first, 1)


class Grace(_Base):
    def test_young_result_untouched(self):
        self._result(age=30.0)                  # younger than ORPHAN_GRACE_S
        self._sweep()
        self.assertTrue((gw.RESULTS_DIR / f"{TID}.txt").exists())
        self.assertEqual(self.posted, [])


class InflightUntouched(_Base):
    def test_ledger_tracked_result_is_not_the_sweeps_business(self):
        self._result()
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results({TID})
        self.assertTrue((gw.RESULTS_DIR / f"{TID}.txt").exists())
        self.assertEqual(self.posted, [])


class MarkerParity(_Base):
    def _archived_task(self):
        d = gw.TASKS_DIR / "archive"
        d.mkdir(parents=True, exist_ok=True)
        t = d / f"{TID}.txt"
        t.write_text(f"id: {TID}\ntask: hi\n")
        _age(t, OLD)

    def test_no_send_orphan_posts_verbatim_so_server_suppresses(self):
        self._archived_task()
        self._result("[no-send]\ninternal only")
        self._sweep()
        self.assertEqual(len(self.posted), 1, "lease still closes via POST")
        body = self.posted[0][2]["body"]
        self.assertIn("[no-send]", body, "marker must reach the server intact")
        self.assertNotIn("recovered result", body,
                         "a suppressed result gets no user-facing label")

    def test_redirect_is_restitched(self):
        self._archived_task()
        self._result("[channel: C123ABC]\nthe reply")
        self._sweep()
        body = self.posted[0][2]["body"]
        self.assertTrue(body.startswith("[channel: C123ABC]"),
                        f"redirect must be first line, got {body[:40]!r}")
        self.assertIn("recovered result", body)

    def test_attachment_orphan_quarantines_for_manual_handling(self):
        self._archived_task()
        self._result("see file [file: /tmp/x.png]")
        self._sweep()
        self.assertEqual(self.posted, [], "never deliver without the files")
        q = list(gw.UNDELIVERABLE_RESULTS_DIR.glob(f"{TID}.has-attachments.*"))
        self.assertEqual(len(q), 1)


class ArchiveConventions(_Base):
    def test_month_partitioned_bare_name_counts_as_delivered(self):
        d = gw.ARCHIVE_RESULTS_DIR / "2026-08"
        d.mkdir(parents=True)
        (d / f"{TID}.txt").write_text("the reply")
        self._result("Replied in room — completion note.")
        self._sweep()
        self.assertEqual(self.posted, [],
                         "month-nested bare-name copy IS a delivered copy")
        self.assertEqual(len(list(gw.ARCHIVE_RESULTS_DIR.glob(
            f"{TID}-*-late-duplicate.txt"))), 1)


class QuarantineCollision(_Base):
    def test_prior_evidence_never_replaced(self):
        gw.UNDELIVERABLE_RESULTS_DIR.mkdir(parents=True)
        self._result("new orphan")
        self._sweep()                            # no task -> quarantine #1
        first = list(gw.UNDELIVERABLE_RESULTS_DIR.glob(f"{TID}.no-task.*"))
        self.assertEqual(len(first), 1)
        self._result("second orphan, same tid")
        gw._orphan_quarantine_logged.clear()
        self._sweep()                            # quarantine #2, same tid
        both = sorted(f.read_text() for f in
                      gw.UNDELIVERABLE_RESULTS_DIR.glob(f"{TID}.no-task.*"))
        self.assertEqual(both, ["new orphan", "second orphan, same tid"],
                         "both quarantined bodies must survive")


class AbandonedHardening(_Base):
    def _drop_probe(self):
        """Two passes of _reconcile_abandoned over one id with no task/result
        pending; returns whether the id survived in the ledger."""
        inflight = {TID}
        suspects = gw._reconcile_abandoned(inflight, set())
        gw._reconcile_abandoned(inflight, suspects)
        return TID in inflight

    def test_recently_archived_task_keeps_the_id(self):
        d = gw.TASKS_DIR / "archive"
        d.mkdir(parents=True)
        t = d / f"{TID}.txt"
        t.write_text("id: x\n")                 # archived seconds ago
        self._saved_save = gw._save_inflight
        gw._save_inflight = lambda s: None
        try:
            self.assertTrue(self._drop_probe(),
                            "completed-HERE minutes ago: the result may still come")
            _age(t, OLD)                        # now old: completed long ago
            self.assertFalse(self._drop_probe(),
                             "old archived task: normal abandoned-drop applies")
        finally:
            gw._save_inflight = self._saved_save


if __name__ == "__main__":
    unittest.main(verbosity=2)

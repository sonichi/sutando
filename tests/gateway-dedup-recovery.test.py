#!/usr/bin/env python3
"""The gateway recovers a dedup whose holder delivered nothing.

Drives the real `_post_ready_results` through all three outcomes: honour a
substantiated dedup, re-ask an unsubstantiated one, and report once re-asking
has already failed.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

TID = "task-633325612fbde6e777"
HOLDER = "task-22d83e59601f3a1fef"
ROOM = "!room:ag2.space"


class _Harness:
    # DEDUP_ALIAS_FILE points at the operator's real state dir; unisolated,
    # these tests would overwrite a live bridge's delivery aliases.
    _PATCH = ("RESULTS_DIR", "ARCHIVE_RESULTS_DIR", "TASKS_DIR", "DEDUP_ALIAS_FILE",
              "_req", "_save_inflight", "_forget_task_room", "_load_task_rooms",
              "_save_task_rooms")

    def __init__(self, gw, tmp: Path):
        self.gw, self.tmp, self.posts, self._saved = gw, tmp, [], {}
        self.rooms = {TID: ROOM}

    def __enter__(self):
        gw = self.gw
        for n in self._PATCH:
            self._saved[n] = getattr(gw, n)
        results, tasks = self.tmp / "results", self.tmp / "tasks"
        (results / "archive").mkdir(parents=True)
        tasks.mkdir(parents=True)
        gw.RESULTS_DIR, gw.ARCHIVE_RESULTS_DIR, gw.TASKS_DIR = results, results / "archive", tasks
        state = self.tmp / "state"
        state.mkdir(parents=True, exist_ok=True)
        gw.DEDUP_ALIAS_FILE = state / "remote-dedup-alias.json"
        gw._req = lambda m, p, payload=None, **k: (
            self.posts.append({"path": p, "payload": payload}) or {})
        gw._save_inflight = lambda *a, **k: None
        gw._forget_task_room = lambda *a, **k: None
        gw._load_task_rooms = lambda *a, **k: dict(self.rooms)
        gw._save_task_rooms = lambda r, *a, **k: self.rooms.update(r)
        return self

    def __exit__(self, *exc):
        for n, v in self._saved.items():
            setattr(self.gw, n, v)
        return False

    def seed(self, holder_body: str, orig_task: str, result_body: str):
        (self.gw.ARCHIVE_RESULTS_DIR / f"{HOLDER}-1785976425.txt").write_text(holder_body)
        (self.gw.TASKS_DIR / f"{TID}.txt").write_text(orig_task)
        (self.gw.RESULTS_DIR / f"{TID}.txt").write_text(result_body)

    def requeued_tasks(self):
        return [p for p in self.gw.TASKS_DIR.glob("task-*.txt") if p.stem != TID]


ORIG = f"id: {TID}\nsource: gateway\naccess_tier: owner\ntask: What is AG2Space?\n"
DEDUP = f"[deduped: {HOLDER}]"


class GatewayDedupRecoveryTest(unittest.TestCase):
    def setUp(self):
        try:
            from ag2_sparrow import remote_gateway_bridge as gw
        except (Exception, SystemExit) as e:  # noqa: BLE001
            self.skipTest(f"gateway not importable: {str(e)[:60]}")
        self.gw = gw

    def _run(self, holder_body, orig=ORIG):
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed(holder_body, orig, DEDUP)
                inflight = {TID}
                self.gw._post_ready_results(inflight)
                return {
                    "posts": list(h.posts),
                    "inflight": set(inflight),
                    "requeued": [p.read_text() for p in h.requeued_tasks()],
                }

    def test_holder_that_answered_is_still_honoured(self):
        """Honoured consolidation: no re-ask, but the raw [deduped:] body must
        still POST — only add_result closes the server-side lease, and the
        server's parse_result suppresses the delivery for the marker."""
        r = self._run("the full answer")
        self.assertEqual(len(r["posts"]), 1, "honoured dedup must POST to close the lease")
        self.assertIn("[deduped:", str(r["posts"][0]["payload"].get("body", "")),
                      "marker must stay intact so the server suppresses delivery")
        self.assertEqual(r["requeued"], [], "honoured dedup should not re-ask")
        self.assertNotIn(TID, r["inflight"])

    def test_empty_holder_is_re_asked(self):
        r = self._run("")
        self.assertEqual(len(r["requeued"]), 1, "the question was not re-asked")
        body = r["requeued"][0]
        self.assertIn("What is AG2Space?", body, "re-ask lost the original question")
        self.assertIn("delivered nothing", body, "re-ask does not tell the core why")
        self.assertIn("dedup_requeue_count: 1", body, "loop guard not stamped")
        self.assertEqual(r["posts"], [], "re-ask should not also post a reply")

    def test_second_failure_reports_and_does_not_loop(self):
        orig = ORIG + "dedup_requeue_count: 1\n"
        r = self._run("", orig=orig)
        self.assertEqual(r["requeued"], [], "looped instead of reporting")
        self.assertEqual(len(r["posts"]), 1, "owner was left with no reply at all")
        self.assertIn("needs a direct answer", r["posts"][0]["payload"]["body"])
        self.assertNotIn(TID, r["inflight"], "task left in flight after reporting")

    def test_recovered_answer_is_delivered_under_the_original_id(self):
        """Two passes. The re-ask is worthless if its answer never ships.

        The broker is waiting on the ORIGINAL delivery id, so the recovered
        answer must POST under that id, not the re-ask's local one.
        """
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG, DEDUP)
                inflight = {TID}

                self.gw._post_ready_results(inflight)          # pass 1: re-ask
                requeued = h.requeued_tasks()
                self.assertEqual(len(requeued), 1, "no re-ask written")
                new_id = requeued[0].stem
                self.assertTrue(
                    inflight,
                    "in-flight is empty after the re-ask — nothing will ever look "
                    "for the recovered answer")

                # The core answers the re-asked task.
                (h.gw.RESULTS_DIR / f"{new_id}.txt").write_text("the recovered answer")
                self.gw._post_ready_results(inflight)          # pass 2: deliver

                bodies = [p["payload"]["body"] for p in h.posts]
                self.assertIn(
                    "the recovered answer", bodies,
                    f"recovered answer was never delivered (posts={h.posts})")
                ids = [p["payload"]["id"] for p in h.posts
                       if p["payload"]["body"] == "the recovered answer"]
                self.assertEqual(
                    ids, [self.gw._broker_tid(TID)],
                    "recovered answer POSTed under the re-ask id; the broker is "
                    "waiting on the original delivery id")

    def test_alias_survives_a_restart(self):
        """The re-ask may outlive the process; the mapping must persist."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG, DEDUP)
                inflight = {TID}
                self.gw._post_ready_results(inflight)
                new_id = h.requeued_tasks()[0].stem
                # Simulate a restart: nothing in memory, ledger reloaded.
                self.assertEqual(
                    self.gw._delivery_tid(new_id), self.gw._delivery_tid(TID),
                    "alias did not persist — after a restart the recovered answer "
                    "would POST under an id the broker does not know")

    def test_alias_write_failure_keeps_the_original_delivery(self):
        """The alias is delivery-critical: if it cannot commit, retire nothing.

        Reviewer scenario — a failing alias save previously still returned
        `requeue`, so the original was archived and the recovered answer POSTed
        under an id the broker was not waiting on.
        """
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG, DEDUP)
                # Unwritable alias path: parent is a file, so mkdir/replace fail.
                blocked = Path(td) / "blocked"
                blocked.write_text("not a directory")
                self.gw.DEDUP_ALIAS_FILE = blocked / "alias.json"

                inflight = {TID}
                self.gw._post_ready_results(inflight)

                self.assertIn(TID, inflight,
                              "original delivery retired despite an uncommitted alias")
                self.assertTrue((h.gw.RESULTS_DIR / f"{TID}.txt").exists(),
                                "result archived despite an uncommitted alias")
                self.assertEqual(list(h.gw.ARCHIVE_RESULTS_DIR.glob(f"{TID}-*.txt")), [],
                                 "archived an unrecoverable dedup")
                self.assertEqual(h.posts, [], "posted despite deferring")

    def test_failed_report_post_is_retried_not_dropped(self):
        """The report IS the delivery. A failed POST must not archive the ask."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG + "dedup_requeue_count: 1\n", DEDUP)  # -> report

                import urllib.error

                def _offline(*a, **k):
                    raise urllib.error.URLError("offline")

                h.gw._req = _offline
                inflight = {TID}
                self.gw._post_ready_results(inflight)

                self.assertIn(TID, inflight,
                              "task dropped from in-flight after a failed report")
                self.assertTrue((h.gw.RESULTS_DIR / f"{TID}.txt").exists(),
                                "result archived after a failed report — ask is stranded")
                self.assertEqual(list(h.gw.ARCHIVE_RESULTS_DIR.glob(f"{TID}-*.txt")), [],
                                 "archived after a failed report")

    def test_corrupt_alias_ledger_defers_instead_of_guessing(self):
        """An unreadable ledger must not fall back to the re-ask id."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG, DEDUP)
                inflight = {TID}
                self.gw._post_ready_results(inflight)          # pass 1: re-ask
                new_id = h.requeued_tasks()[0].stem
                h.gw.DEDUP_ALIAS_FILE.write_text("{")           # corrupt it
                (h.gw.RESULTS_DIR / f"{new_id}.txt").write_text("the recovered answer")
                h.posts.clear()
                self.gw._post_ready_results(inflight)          # pass 2

                self.assertEqual(h.posts, [],
                                 "POSTed under a guessed id with an unreadable ledger")
                self.assertIn(new_id, inflight, "dropped the re-ask on a ledger failure")
                self.assertTrue((h.gw.RESULTS_DIR / f"{new_id}.txt").exists(),
                                "archived the recovered answer it could not address")

    def test_failed_identity_commit_publishes_no_task(self):
        """A re-ask must not exist before its routing does, or it orphans."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.seed("", ORIG, DEDUP)
                blocked = Path(td) / "blocked"
                blocked.write_text("not a directory")
                h.gw.DEDUP_ALIAS_FILE = blocked / "alias.json"

                inflight = {TID}
                self.gw._post_ready_results(inflight)
                self.assertEqual(h.requeued_tasks(), [],
                                 "published a re-ask whose routing never committed")
                self.gw._post_ready_results(inflight)
                self.assertEqual(len(h.requeued_tasks()), 0,
                                 "second pass added another orphan re-ask")
                self.assertIn(TID, inflight)

    def test_report_is_sent_even_with_an_empty_room_map(self):
        """The results endpoint is keyed by delivery id, not by room."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                h.rooms = {}                                    # sidecar lost
                h.seed("", ORIG + "dedup_requeue_count: 1\n", DEDUP)
                inflight = {TID}
                self.gw._post_ready_results(inflight)
                self.assertEqual(len(h.posts), 1,
                                 "report silently dropped when the room map was empty")
                self.assertEqual(h.posts[0]["payload"]["id"], self.gw._broker_tid(TID))

    def test_unknown_holder_is_re_asked(self):
        """No archived record of the holder is not evidence that it answered."""
        with tempfile.TemporaryDirectory() as td:
            with _Harness(self.gw, Path(td)) as h:
                (h.gw.TASKS_DIR / f"{TID}.txt").write_text(ORIG)
                (h.gw.RESULTS_DIR / f"{TID}.txt").write_text(DEDUP)
                inflight = {TID}
                self.gw._post_ready_results(inflight)
                self.assertEqual(len(h.requeued_tasks()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

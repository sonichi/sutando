"""Projector contract: CREATE then EDIT, idempotent on the revision ledger,
retry-whole on a rejected send, dedupe keys stable per (requirement, revision).
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl import manager as hm  # noqa: E402
from hitl.manager import HitlManager, HitlStore, archive_health  # noqa: E402
from hitl.projector import (  # noqa: E402
    fallback_body,
    pending_ids,
    project,
)
from hitl.schema import (  # noqa: E402
    Action, CATEGORY_BLOCKED, CATEGORY_DECISION, HumanRequirement, KINDS,
    STATUS_RESOLVED, WIRE_FIELD, category_of,
)

ROOM = "!room:ag2.space"


def make_req(**kw):
    defaults = dict(
        kind="auth",
        runtime="claude",
        message="Claude Code needs to sign in again",
        guard="g1",
        device={"id": "d1", "name": "Qingyun's Air"},
        actions=[Action(id="reauth", kind="authenticate", label="Re-authenticate")],
    )
    defaults.update(kw)
    return HumanRequirement(**defaults)


class RecordingSender:
    def __init__(self):
        self.sent = []
        self.fail_next = False
        self.counter = 0

    def __call__(self, payload):
        self.sent.append(payload)
        if self.fail_next:
            self.fail_next = False
            return {"ok": False}
        self.counter += 1
        return {"ok": True, "event_id": f"$ev{self.counter}"}


class ProjectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        self.send = RecordingSender()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_then_edit(self):
        req = self.mgr.create(make_req())
        done = project(self.mgr, self.send, ROOM)
        self.assertEqual(done, [(req.id, "$ev1")])
        first = self.send.sent[0]
        self.assertEqual(first["op"], "message")
        self.assertEqual(first["room_id"], ROOM)
        self.assertEqual(first["dedupe_key"], f"hitl:{req.id}:1")
        wire = first["extra_content"][WIRE_FIELD]
        self.assertEqual(wire["status"], "pending")
        self.assertEqual(wire["revision"], 1)
        self.assertIn("Sutando needs your attention", first["body"])
        self.assertIn("Qingyun's Air", first["body"])

        self.mgr.resolve(req.id)
        project(self.mgr, self.send, ROOM)
        edit = self.send.sent[1]
        self.assertEqual(edit["op"], "edit")
        self.assertEqual(edit["event_id"], "$ev1")  # EDIT targets the CREATE
        self.assertEqual(edit["dedupe_key"], f"hitl:{req.id}:2")
        self.assertEqual(edit["extra_content"][WIRE_FIELD]["status"], "resolved")
        self.assertIn("Resolved", edit["body"])

    def test_idempotent_between_changes(self):
        req = self.mgr.create(make_req())
        project(self.mgr, self.send, ROOM)
        self.assertEqual(project(self.mgr, self.send, ROOM), [])
        self.assertEqual(len(self.send.sent), 1)  # nothing re-sent

    def test_rejected_send_retries_whole(self):
        req = self.mgr.create(make_req())
        self.send.fail_next = True
        self.assertEqual(project(self.mgr, self.send, ROOM), [])
        self.assertTrue(self.mgr.needs_projection(req.id))  # nothing recorded
        done = project(self.mgr, self.send, ROOM)
        self.assertEqual(done, [(req.id, "$ev1")])
        # Same dedupe key on both attempts: the gateway absorbs a send that
        # failed after landing. (The fake numbers successes only, hence $ev1.)
        self.assertEqual(self.send.sent[0]["dedupe_key"], self.send.sent[1]["dedupe_key"])

    def test_edit_target_survives_multiple_transitions(self):
        req = self.mgr.create(make_req())
        project(self.mgr, self.send, ROOM)
        loaded = self.mgr.get(req.id)
        loaded.refresh_guard("g2")
        self.mgr.store.save(loaded)
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        project(self.mgr, self.send, ROOM)
        ops = [(p["op"], p.get("event_id")) for p in self.send.sent
               if WIRE_FIELD in p.get("extra_content", {})]
        self.assertEqual(ops, [("message", None), ("edit", "$ev1"), ("edit", "$ev1")])

    def test_fallback_body_terminal_states(self):
        req = make_req()
        req.transition("cancelled")
        self.assertIn("Cancelled", fallback_body(req))

    def test_a_decision_does_not_wear_the_blocking_header(self):
        """The card colours choice/confirmation apart from a real block; a client
        that ignores the card field used to get the alarm header for every kind."""
        for kind in ("choice", "confirmation"):
            body = fallback_body(make_req(kind=kind))
            self.assertNotIn("⚠", body, kind)
            self.assertIn("needs a decision", body, kind)

    def test_a_real_block_keeps_the_alarm(self):
        for kind in ("auth", "permission", "billing", "external_action"):
            body = fallback_body(make_req(kind=kind))
            self.assertIn("⚠", body, kind)
            self.assertIn("needs your attention", body, kind)

    def test_an_unrecognised_kind_falls_back_to_blocked(self):
        """Under-stating a block strands the user; over-warning only annoys."""
        self.assertEqual(category_of("nonsense"), CATEGORY_BLOCKED)
        self.assertIn("⚠", fallback_body(make_req(kind="unknown")))

    def test_every_declared_kind_has_a_head(self):
        """A kind added to KINDS without a category would KeyError at send time."""
        for kind in KINDS:
            self.assertIn(category_of(kind), (CATEGORY_BLOCKED, CATEGORY_DECISION), kind)

    def test_a_kind_mapped_to_a_head_less_category_still_renders(self):
        """The direct index was total only because construction coerces an
        UNRECOGNISED kind to `unknown` — so the KeyError is unreachable that
        way, and sonichi's first probe measured the coercion instead of the
        renderer. It becomes reachable when a kind already IN `KINDS` is mapped
        to a third category, which is why this remaps `choice` rather than
        inventing one, and why the fallback is BLOCKED: raising here would
        raise inside the renderer that exists for clients which cannot read
        the structured field.
        """
        import hitl.schema as S
        original = dict(S._KIND_CATEGORY)
        S._KIND_CATEGORY["choice"] = "escalation"          # a category with no head
        try:
            req = make_req(kind="choice")
            self.assertEqual(req.kind, "choice", "precondition: not coerced away")
            self.assertEqual(category_of("choice"), "escalation")
            body = fallback_body(req)
            self.assertIn("⚠", body)
            self.assertIn("needs your attention", body)
        finally:
            S._KIND_CATEGORY.clear()
            S._KIND_CATEGORY.update(original)
        self.assertEqual(category_of("choice"), CATEGORY_DECISION, "restored")


class FallbackWhereTests(unittest.TestCase):
    def test_the_plain_text_says_which_machine_when_the_host_is_known(self):
        import hitl.schema as SCHEMA
        from hitl.projector import fallback_body
        with mock.patch.object(SCHEMA, "device_host", return_value="Chis-MacBook-Pro"):
            self.assertIn("(on Chis-MacBook-Pro · Qingyun's Air)", fallback_body(make_req()))
        with mock.patch.object(SCHEMA, "device_host", return_value=""):
            self.assertIn("(on Qingyun's Air)", fallback_body(make_req()))
            self.assertNotIn("(on ", fallback_body(make_req(device=None)))


class ArchiveTests(unittest.TestCase):
    """A settled record leaves the live set, so the per-pulse scan does not grow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "requirements"
        self.mgr = HitlManager(HitlStore(self.root))
        self.send = RecordingSender()

    def tearDown(self):
        self.tmp.cleanup()

    def live(self):
        return sorted(p.stem for p in self.root.glob("hitl_*.json"))

    def settled(self, kind="choice"):
        req = self.mgr.create(make_req(kind=kind))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        project(self.mgr, self.send, ROOM)
        return req

    def test_resolved_and_caught_up_is_archived(self):
        req = self.settled()
        self.assertEqual(pending_ids(self.mgr), [])
        self.assertEqual(self.live(), [])
        self.assertTrue((self.mgr.store.archive_root / f"{req.id}.json").exists())
        self.assertEqual(self.mgr.store.archive_root, self.root.with_name("requirements-archive"))

    def test_a_card_whose_recovery_was_sent_is_archived(self):
        req = self.settled(kind="auth")
        self.assertTrue(self.mgr.store.projection(req.id).get("recovered"))
        pending_ids(self.mgr)
        self.assertEqual(self.live(), [])

    def test_archived_record_still_loads(self):
        req = self.settled()
        pending_ids(self.mgr)
        got = self.mgr.get(req.id)
        self.assertIsNotNone(got)
        self.assertEqual(got.status, STATUS_RESOLVED)
        self.assertEqual(self.mgr.store.projection(req.id)["event_id"], "$ev1")
        self.assertEqual(self.mgr.resolve(req.id), [])  # a late click: already answered
        self.assertEqual(self.live(), [])

    def test_all_no_longer_returns_archived(self):
        done = self.settled()
        open_ = self.mgr.create(make_req(guard="g2", device={"id": "d2"}))
        pending_ids(self.mgr)
        self.assertEqual([r.id for r in self.mgr.store.all()], [open_.id])
        self.assertNotEqual(done.id, open_.id)

    def test_resolved_with_edit_owed_stays(self):
        req = self.mgr.create(make_req(kind="choice"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        self.assertEqual(pending_ids(self.mgr), [req.id])
        self.assertEqual(self.live(), [req.id])

    def test_resolved_with_recovery_owed_stays(self):
        req = self.mgr.create(make_req(kind="auth"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        refuse_notices = lambda payload: (  # noqa: E731
            self.send(payload) if WIRE_FIELD in payload["extra_content"] else {"ok": False})
        project(self.mgr, refuse_notices, ROOM)
        self.assertFalse(self.mgr.needs_projection(req.id))
        self.assertEqual(pending_ids(self.mgr), [req.id])
        self.assertEqual(self.live(), [req.id])

    def test_open_stays(self):
        req = self.mgr.create(make_req(kind="choice"))
        project(self.mgr, self.send, ROOM)
        self.assertEqual(pending_ids(self.mgr), [])
        self.assertEqual(self.live(), [req.id])

    def test_the_move_waits_for_the_store_lock(self):
        req = self.settled()
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(0.6)",
             str(self.root / ".lock")],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            start = time.monotonic()
            self.assertTrue(self.mgr.archive_if_settled(req.id, time.time()))
            self.assertGreaterEqual(time.monotonic() - start, 0.3)
        finally:
            holder.wait(timeout=5)
            holder.stdout.close()
        self.assertEqual(self.live(), [])
        self.assertIsNotNone(self.mgr.get(req.id))

    def test_a_failed_archive_never_blocks_delivery(self):
        settled = self.settled()
        self.mgr.store.archive_root.write_text("not a dir")
        card = self.mgr.create(make_req(kind="choice", guard="g2"))
        with self.assertLogs("hitl.store", level="WARNING"):
            self.assertEqual(pending_ids(self.mgr), [card.id])
        self.assertEqual(self.live(), sorted([settled.id, card.id]))

    def test_a_persistent_failure_warns_once_per_id(self):
        a = self.settled()
        b = self.mgr.create(make_req(kind="choice", guard="gb"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(b.id)
        project(self.mgr, self.send, ROOM)
        self.mgr.store.archive_root.write_text("not a dir")
        with self.assertLogs("hitl.store", level="WARNING") as logs:
            pending_ids(self.mgr)
        self.assertEqual(len(logs.records), 2)
        for _ in range(3):  # later pulses, and a fresh store object as the gateway makes
            with self.assertNoLogs("hitl.store"):
                pending_ids(HitlManager(HitlStore(self.root)))
        self.assertEqual(self.live(), sorted([a.id, b.id]))

    def test_failures_are_published_for_health_and_clear_when_fixed(self):
        self.settled()
        self.mgr.store.archive_root.write_text("not a dir")
        with self.assertLogs("hitl.store", level="WARNING"):
            pending_ids(self.mgr)
        h = archive_health(self.root)
        self.assertEqual((h["live"], h["failures"]), (1, 1))
        self.assertIn("FileExistsError", h["error"])
        self.mgr.store.archive_root.unlink()
        pending_ids(self.mgr)
        self.assertEqual((archive_health(self.root)["live"], archive_health(self.root)["failures"]),
                         (0, 0))

    def test_a_failing_record_that_leaves_the_live_set_stops_counting(self):
        req = self.settled()
        self.mgr.store.archive_root.write_text("not a dir")
        with self.assertLogs("hitl.store", level="WARNING"):
            pending_ids(self.mgr)
        self.mgr.store._path(req.id).unlink()
        pending_ids(self.mgr)
        self.assertEqual(archive_health(self.root)["failures"], 0)

    def test_a_healthy_store_publishes_zero_failures_outside_the_store(self):
        self.settled()
        pending_ids(self.mgr)
        self.assertEqual(archive_health(self.root)["failures"], 0)
        status = hm.archive_status_path(self.root)
        self.assertTrue(status.exists())
        self.assertEqual(status.parent, self.root.parent)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name != ".lock"), [])
        self.assertEqual(self.mgr.store.all(), [])
        self.assertIsNone(archive_health(self.root.parent / "absent"))

    def test_the_status_is_written_only_when_it_changes(self):
        self.settled()
        self.mgr.store.archive_root.write_text("not a dir")
        with self.assertLogs("hitl.store", level="WARNING"):
            pending_ids(self.mgr)
        status = hm.archive_status_path(self.root)
        status.unlink()
        pending_ids(self.mgr)
        self.assertFalse(status.exists())
        self.mgr.store.archive_root.unlink()
        pending_ids(self.mgr)
        self.assertEqual(json.loads(status.read_text())["failures"], 0)

    def test_a_new_error_text_rewrites_the_status(self):
        store = self.mgr.store
        store.report_archive_failures({"hitl_a": PermissionError("denied")}, 1000.0)
        store.report_archive_failures({"hitl_a": OSError("disk full")}, 1001.0)
        h = archive_health(self.root, now=1002.0)
        self.assertEqual((h["failures"], h["error"]), (1, "OSError: disk full"))

    def test_a_steady_status_is_refreshed_and_a_stopped_writer_reads_stale(self):
        self.assertLess(hm.ARCHIVE_STATUS_REFRESH_S, hm.ARCHIVE_STATUS_STALE_S)
        for failures in ({"hitl_a": PermissionError("denied")}, {}):
            store = HitlStore(self.root / f"r{len(failures)}")
            with self.subTest(failures=len(failures)):
                store.report_archive_failures(failures, 1000.0)
                t = 1000.0 + hm.ARCHIVE_STATUS_REFRESH_S
                store.report_archive_failures(failures, t - 1)
                self.assertEqual(archive_health(store.root, now=1000.0)["updated_at"], 1000.0)
                store.report_archive_failures(failures, t)
                self.assertEqual(archive_health(store.root, now=t)["updated_at"], t)
                stale = hm.ARCHIVE_STATUS_STALE_S
                self.assertFalse(archive_health(store.root, now=t + stale)["stale"])
                self.assertTrue(archive_health(store.root, now=t + stale + 1)["stale"])

    def test_a_report_without_updated_at_reads_stale(self):
        hm.archive_status_path(self.root).write_text(json.dumps({"failures": 2, "error": "x"}))
        self.assertTrue(archive_health(self.root)["stale"])

    def test_no_report_or_a_non_object_report_reads_as_not_reported(self):
        self.assertEqual(
            (archive_health(self.root)["reported"], archive_health(self.root)["stale"]), (False, False))
        hm.archive_status_path(self.root).write_text("[1]")
        h = archive_health(self.root)
        self.assertEqual((h["reported"], h["stale"], h["failures"]), (False, False, 0))

    def test_a_failed_status_write_leaves_no_temp_and_retries(self):
        store = self.mgr.store
        with mock.patch("hitl.manager.os.replace", side_effect=OSError("read-only")):
            store.report_archive_failures({}, 1.0)
        status = hm.archive_status_path(self.root)
        self.assertEqual(sorted(p.name for p in status.parent.iterdir()
                                if p.name.startswith(".tmp-archive-status-")), [])
        self.assertFalse(status.exists())
        store.report_archive_failures({}, 2.0)
        self.assertEqual(json.loads(status.read_text())["updated_at"], 2.0)

    def test_an_id_that_recovers_and_fails_again_warns_again(self):
        store, err = self.mgr.store, {"hitl_a": PermissionError("denied")}
        with self.assertLogs("hitl.store", level="WARNING"):
            store.report_archive_failures(err, 1.0)
        with self.assertNoLogs("hitl.store"):
            store.report_archive_failures(err, 2.0)
            store.report_archive_failures({}, 3.0)
        with self.assertLogs("hitl.store", level="WARNING"):
            store.report_archive_failures(err, 4.0)

    def archive_on_first_read(self, req_id):
        """Read the live file only after another writer has archived it."""
        live, real, store = self.root / f"{req_id}.json", Path.read_text, self.mgr.store

        def read_text(path, *a, **kw):
            if path == live and path.exists():
                store.archive(req_id)
            return real(path, *a, **kw)
        return mock.patch.object(Path, "read_text", read_text)

    def test_load_survives_an_archive_between_check_and_read(self):
        req = self.settled()
        with self.archive_on_first_read(req.id):
            got = self.mgr.get(req.id)
        self.assertEqual(got.status, STATUS_RESOLVED)
        self.assertEqual(self.live(), [])

    def test_all_treats_a_mid_scan_archive_as_moved(self):
        req = self.settled()
        with self.archive_on_first_read(req.id), self.assertNoLogs("hitl.store"):
            self.assertEqual(self.mgr.store.all(), [])
        self.assertEqual(self.mgr.store.last_skipped, ())

    def test_archive_if_settled_refuses_an_open_record(self):
        req = self.mgr.create(make_req(kind="choice"))
        project(self.mgr, self.send, ROOM)
        self.assertFalse(self.mgr.archive_if_settled(req.id, time.time()))
        self.assertEqual(self.live(), [req.id])

    def test_archive_if_settled_refuses_an_owed_edit(self):
        req = self.mgr.create(make_req(kind="choice"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        self.assertFalse(self.mgr.archive_if_settled(req.id, time.time()))
        self.assertEqual(self.live(), [req.id])

    def test_archive_if_settled_refuses_an_owed_recovery(self):
        req = self.mgr.create(make_req(kind="auth"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(req.id)
        self.mgr.record_projection(req.id, self.mgr.get(req.id).revision, None)
        self.assertFalse(self.mgr.needs_projection(req.id))
        self.assertFalse(self.mgr.archive_if_settled(req.id, time.time()))
        self.assertEqual(self.live(), [req.id])

    def test_cancelled_and_expired_are_archived(self):
        a = self.mgr.create(make_req(kind="choice", guard="ga"))
        b = self.mgr.create(make_req(kind="choice", guard="gb"))
        project(self.mgr, self.send, ROOM)
        self.mgr.cancel(a.id)
        self.mgr.expire(b.id)
        project(self.mgr, self.send, ROOM)
        self.assertEqual(pending_ids(self.mgr), [])
        self.assertEqual(self.live(), [])
        self.assertEqual(self.mgr.get(a.id).status, "cancelled")
        self.assertEqual(self.mgr.get(b.id).status, "expired")

    def test_recovery_owed_beside_an_open_sibling_stays(self):
        a = self.mgr.create(make_req(kind="core-blocked", guard="ga"))
        b = self.mgr.create(make_req(kind="core-blocked", guard="gb"))
        project(self.mgr, self.send, ROOM)
        self.mgr.resolve(a.id)
        project(self.mgr, self.send, ROOM)
        self.assertFalse(self.mgr.needs_projection(a.id))
        pending_ids(self.mgr)
        self.assertEqual(self.live(), sorted([a.id, b.id]))
        self.mgr.resolve(b.id)
        project(self.mgr, self.send, ROOM)  # b's edit, then one recovery for the seat
        self.assertEqual(pending_ids(self.mgr), [])
        self.assertEqual(self.live(), [])

    def test_linking_a_task_never_revives_an_archived_record(self):
        req = self.settled()
        pending_ids(self.mgr)
        self.mgr.link_blocked_task(req.id, "task-late")
        self.assertEqual(self.live(), [])
        self.assertNotIn("task-late", self.mgr.get(req.id).blocked_task_ids)


if __name__ == "__main__":
    unittest.main()

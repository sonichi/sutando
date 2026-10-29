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
    NOTICE_FIELD, STATUS_RESOLVED, WIRE_FIELD, category_of,
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
        self.assertIn("✓ Signed in — Sutando has continued its work.", edit["body"])
        self.assertNotIn("econnect", edit["body"])

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

    def test_only_a_cleared_login_reads_signed_in(self):
        for kind, line in (("auth", "✓ Signed in — Sutando has continued its work."),
                           ("core-blocked", "✓ Resolved — Sutando has continued its work.")):
            req = make_req(kind=kind)
            req.transition("resolved")
            self.assertIn(line, fallback_body(req))

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



class RefusalClassTests(unittest.TestCase):
    """Only a 4xx about the payload is the card's own; auth, timeouts, rate limits, 5xx and network
    errors are the sender's and propagate to the caller's backoff with nothing recorded."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))

    def _raising(self, code):
        def send(payload):
            err = OSError(f"HTTP {code}")
            err.code = code
            raise err
        return send

    def test_transport_refusals_propagate_and_record_nothing(self):
        for code in (401, 403, 404, 405, 408, 409, 410, 429, 500, 503):
            with self.subTest(code=code):
                req = self.mgr.create(make_req(guard=f"g{code}", device={"id": f"d{code}", "name": "n"}))
                with self.assertRaises(OSError):
                    project(self.mgr, self._raising(code), ROOM)
                ledger = self.mgr.store.projection(req.id)
                self.assertNotIn("refusals", ledger)
                self.assertTrue(self.mgr.needs_projection(req.id))
                self.mgr.cancel(req.id)
                self.mgr.record_projection(req.id, 99, "$x")

    def test_a_refused_card_is_retried_at_its_next_revision(self):
        from hitl import projector as pj
        self.addCleanup(pj._NO_ACCEPT_SINCE.clear)
        req = self.mgr.create(make_req())
        for i in range(pj.PROJECTION_MAX_REFUSALS + 1):  # the first falls inside the grace
            later = lambda: 10.0 ** 7 * (i + 1)  # noqa: E731
            with self.assertRaises(OSError):
                pj.project_pass(self.mgr, self._raising(400), ROOM, clock=lambda: time.time() + later(),
                                monotonic=lambda: time.monotonic() + later())
        self.assertTrue(self.mgr.projection_failed(req.id))
        self.assertFalse(self.mgr.needs_projection(req.id))
        self.assertEqual(pending_ids(self.mgr), [req.id], "a failed card owes only its failure notice")
        self.mgr.resolve(req.id)
        self.assertTrue(self.mgr.needs_projection(req.id), "a new revision gets a fresh try")
        sent = RecordingSender()
        self.assertEqual([i for i, _ in project(self.mgr, sent, ROOM)], [req.id])
        self.assertNotIn("failed_revision", self.mgr.store.projection(req.id))



def _refusing(code):
    def send(payload):
        err = OSError(f"HTTP {code}")
        err.code = code
        raise err
    return send


class RefusalWriterTests(unittest.TestCase):
    """The refusal count, the bound and the revision fence are one writer transaction, and the
    retry wait survives a wall-clock spike and a restart without freezing a card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        from hitl import projector as pj
        self.pj = pj
        self.addCleanup(pj._RETRY_DEADLINES.clear)
        self.addCleanup(pj._NO_ACCEPT_SINCE.clear)

    def _pass(self, send, wall, mono):
        try:
            return self.pj.project_pass(self.mgr, send, ROOM, clock=lambda: wall, monotonic=lambda: mono)
        except OSError as e:  # nothing accepted: re-raised for the caller's backoff, pass attached
            return e.projection

    def test_an_endpoint_wide_404_fails_no_card_and_recovers(self):
        a = self.mgr.create(make_req(guard="a", device={"id": "da", "name": "n"}))
        b = self.mgr.create(make_req(guard="b", device={"id": "db", "name": "n"}))
        for i in range(self.pj.PROJECTION_MAX_REFUSALS * 2):
            with self.assertRaises(OSError):
                project(self.mgr, _refusing(404), ROOM)
        for req in (a, b):
            self.assertFalse(self.mgr.projection_failed(req.id))
            self.assertNotIn("refusals", self.mgr.store.projection(req.id))
        self.assertEqual(sorted(i for i, _ in project(self.mgr, RecordingSender(), ROOM)), sorted([a.id, b.id]))

    def test_a_revision_accepted_while_its_refusal_is_recorded_is_never_failed(self):
        """Another writer accepts the revision between the refused send and its bookkeeping."""
        import threading
        req = self.mgr.create(make_req())
        n = self.pj.PROJECTION_MAX_REFUSALS
        for i in range(n - 1):
            self._pass(_refusing(400), 1000.0 + 10 ** 6 * i, 10.0 ** 6 * i)
        other = HitlManager(HitlStore(Path(self.tmp.name)))
        armed, threads, real = [], [], self.mgr.store.projection

        def projection(req_id):
            seen = real(req_id)
            if armed:  # the refusal's read is done; the other writer accepts before its write
                armed.clear()
                t = threading.Thread(target=other.record_projection, args=(req.id, req.revision, "$accepted"))
                t.start()
                t.join(1.0)  # blocks only while this pass holds the store lock
                threads.append(t)
            return seen

        self.mgr.store.projection = projection

        def send(payload):
            armed.append(1)
            _refusing(400)(payload)

        self._pass(send, 1000.0 + 10 ** 6 * n, 10.0 ** 6 * n)
        for t in threads:
            t.join(10)
        self.assertEqual(len(threads), 1, "the accepting writer ran during the refusal's bookkeeping")
        ledger = real(req.id)
        self.assertEqual((ledger["revision"], ledger["event_id"]), (1, "$accepted"))
        self.assertFalse(self.mgr.projection_failed(req.id), "an accepted revision is never failed")

    def test_a_corrected_forward_clock_spike_does_not_freeze_a_refused_card(self):
        req = self.mgr.create(make_req())
        year = 365 * 86400.0
        self._pass(_refusing(400), 1000.0, 0.0)                 # retry in 30 s
        self._pass(_refusing(400), 1000.0 + year + 31, 31.0)    # the second refusal under a spiked clock
        sent = RecordingSender()
        self.assertEqual(self._pass(sent, 1100.0, 31.0 + 61).done, [(req.id, "$ev1")],
                         "once the clock is corrected the card is retried after its real wait")

    def test_after_a_restart_a_deadline_written_under_a_spiked_clock_is_not_trusted(self):
        req = self.mgr.create(make_req())
        year = 365 * 86400.0
        self._pass(_refusing(400), 1000.0 + year, 0.0)
        getattr(self.pj, "_RETRY_DEADLINES", {}).clear()  # a restart: no in-process deadline survives
        self.assertEqual(pending_ids(self.mgr, clock=lambda: 1000.0), [req.id])
        self.assertEqual(len(self._pass(RecordingSender(), 1000.0, 5.0).done), 1)

    def test_two_processes_refused_at_once_both_count(self):
        req = self.mgr.create(make_req())
        root, src = self.tmp.name, str(Path(__file__).resolve().parent.parent / "src")
        child = (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from hitl.manager import HitlManager, HitlStore\n"
            "from hitl import projector\n"
            "root = Path(%r)\n"
            "store = HitlStore(root)\n"
            "real = store.save\n"
            "def save(*a, **k):\n"
            "    (root / ('.at-save-%%d' %% os.getpid())).touch()\n"
            "    end = time.time() + 2\n"
            "    while len(list(root.glob('.at-save-*'))) < 2 and time.time() < end:\n"
            "        time.sleep(0.02)\n"
            "    real(*a, **k)\n"
            "store.save = save\n"
            "projector._NO_ACCEPT_SINCE[str(root)] = time.monotonic() - projector.PROJECTION_ENDPOINT_GRACE_S - 1\n"
            "def send(p):\n"
            "    e = OSError('HTTP 400'); e.code = 400; raise e\n"
            "try:\n"
            "    projector.project_pass(HitlManager(store), send, '!r', monotonic=time.monotonic)\n"
            "except OSError:\n"
            "    pass\n"
        ) % (src, root)
        procs = [subprocess.Popen([sys.executable, "-c", child]) for _ in range(2)]
        self.assertEqual([p.wait(timeout=60) for p in procs], [0, 0])
        self.assertEqual(self.mgr.store.projection(req.id).get("refusals"), 2,
                         "two refused sends are two attempts, whatever their interleaving")



class _RefusalDrive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))
        from hitl import projector as pj
        self.pj = pj
        self.addCleanup(pj._RETRY_DEADLINES.clear)
        self.addCleanup(pj._NO_ACCEPT_SINCE.clear)
        self.t = 0

    def card(self, g):
        return self.mgr.create(make_req(guard=g, device={"id": f"d{g}", "name": "n"}))

    def drive(self, send, step=10 ** 7, describe=None):
        """One pass `step` seconds after the last (by default past every wait); returns (pass, raised)."""
        self.t += step
        wall, mono = time.time() + self.t, time.monotonic() + self.t
        try:
            return self.pj.project_pass(self.mgr, send, ROOM, clock=lambda: wall, monotonic=lambda: mono,
                                        describe=describe), None
        except OSError as e:
            return e.projection, e

    def refused_for(self, *bad):
        ok = RecordingSender()

        def send(payload):
            if payload["extra_content"].get(WIRE_FIELD, {}).get("id") in bad:
                _refusing(400)(payload)
            return ok(payload)
        return send


class EndpointWideRefusalTests(_RefusalDrive):
    """A card-class 4xx is charged at once only beside an accepted card; a pass that accepts
    nothing charges no card within the grace, however many cards it refused, and backs off."""

    def test_an_endpoint_wide_400_fails_no_card_within_the_grace_and_reaches_the_caller(self):
        cards = [self.card(g) for g in ("a", "b", "c")]
        for _ in range(self.pj.PROJECTION_MAX_REFUSALS * 4):
            got, raised = self.drive(_refusing(400), step=10)
            self.assertIsNotNone(raised, "the caller's backoff must engage")
            self.assertTrue(raised.endpoint_wide)
            self.assertEqual((got.refused, got.failed), ([], []))
        for req in cards:
            self.assertFalse(self.mgr.projection_failed(req.id))
            self.assertNotIn("refusals", self.mgr.store.projection(req.id))
        got, raised = self.drive(RecordingSender())
        self.assertIsNone(raised)
        self.assertEqual(sorted(i for i, _ in got.done), sorted(r.id for r in cards))

    def test_an_outage_whose_bodies_carry_request_ids_charges_no_card(self):
        cards = [self.card(g) for g in ("a", "b")]
        ids = iter(range(10 ** 6))
        for _ in range(self.pj.PROJECTION_MAX_REFUSALS * 4):
            _, raised = self.drive(_refusing(400), step=10, describe=lambda e: f"bad request {next(ids)}")
            self.assertTrue(raised.endpoint_wide)
        for req in cards:
            self.assertNotIn("refusals", self.mgr.store.projection(req.id))

    def test_a_defect_two_cards_share_fails_both_once_the_grace_runs_out(self):
        cards = [self.card(g) for g in ("a", "b")]
        for _ in range(20):
            self.drive(_refusing(400), step=10)
        for req in cards:
            self.assertFalse(self.mgr.projection_failed(req.id), "still within the grace")
        for _ in range(self.pj.PROJECTION_MAX_REFUSALS):
            _, raised = self.drive(_refusing(400))
            self.assertFalse(raised.endpoint_wide)
        for req in cards:
            self.assertTrue(self.mgr.projection_failed(req.id))
        self.assertEqual(sorted(pending_ids(self.mgr)), sorted(r.id for r in cards), "failure notices owed")
        got, raised = self.drive(RecordingSender())
        self.assertIsNone(raised)
        self.assertEqual(sorted(i for i, _ in got.done), sorted(r.id for r in cards))
        self.assertEqual(pending_ids(self.mgr), [])

    def test_an_accepted_send_restarts_the_grace(self):
        a, b = self.card("a"), self.card("b")
        self.drive(_refusing(400), step=10)
        c = self.card("c")
        got, _ = self.drive(self.refused_for(a.id, b.id), step=self.pj.PROJECTION_ENDPOINT_GRACE_S - 10)
        self.assertEqual([i for i, _ in got.done], [c.id])
        d, e = self.card("d"), self.card("e")
        _, raised = self.drive(_refusing(400), step=20)  # a and b still wait out their retry
        self.assertTrue(raised.endpoint_wide, "the grace counts from the first refusal after an accept")
        for req in (d, e):
            self.assertNotIn("refusals", self.mgr.store.projection(req.id))

    def test_a_lone_card_refused_inside_the_grace_is_not_charged_and_reaches_the_caller(self):
        """One card alone cannot tell the relay's fault from its own: the relay gets the grace."""
        req = self.card("a")
        steps = int(self.pj.PROJECTION_ENDPOINT_GRACE_S // 10) - 1
        for _ in range(steps):
            got, raised = self.drive(_refusing(400), step=10)
            self.assertIsNotNone(raised, "the caller's backoff must engage")
            self.assertEqual(raised.code, 400)
            self.assertTrue(raised.endpoint_wide)
            self.assertEqual((got.refused, got.failed), ([], []))
        self.assertGreater(steps, self.pj.PROJECTION_MAX_REFUSALS)
        self.assertNotIn("refusals", self.mgr.store.projection(req.id))
        self.assertFalse(self.mgr.projection_failed(req.id))

    def test_one_card_refused_beside_an_accepted_one_is_that_cards(self):
        bad, good = self.card("a"), self.card("b")
        got, raised = self.drive(self.refused_for(bad.id))
        self.assertIsNone(raised, "a pass that delivered a card is not the relay's failure")
        self.assertEqual([i for i, _ in got.done], [good.id])
        self.assertEqual([(i, n) for i, n, _, _ in got.refused], [(bad.id, 1)])
        self.assertNotIn("refusals", self.mgr.store.projection(good.id))

    def test_a_lone_card_refused_past_the_grace_is_charged_and_fails_within_the_bound(self):
        req = self.card("a")
        _, raised = self.drive(_refusing(400))  # opens the grace
        self.assertTrue(raised.endpoint_wide)
        n = self.pj.PROJECTION_MAX_REFUSALS
        for i in range(n):
            got, raised = self.drive(_refusing(400))
            self.assertIsNotNone(raised)
            self.assertFalse(raised.endpoint_wide)
            self.assertEqual(self.mgr.store.projection(req.id).get("refusals"), i + 1)
        self.assertEqual([i for i, _, _ in got.failed], [req.id])
        self.assertTrue(self.mgr.projection_failed(req.id))
        self.assertFalse(self.mgr.needs_projection(req.id), "given up on this revision")

    def test_a_bad_card_fails_at_the_bound_across_passes_where_others_succeed(self):
        bad = self.card("bad")
        for i in range(self.pj.PROJECTION_MAX_REFUSALS):
            self.card(f"good{i}")
            got, raised = self.drive(self.refused_for(bad.id))
            self.assertIsNone(raised)
        self.assertEqual([i for i, _, _ in got.failed], [bad.id])
        self.assertTrue(self.mgr.projection_failed(bad.id))

    def test_a_card_first_refused_after_the_grace_ran_out_is_charged_at_once(self):
        first = self.card("a")
        for _ in range(self.pj.PROJECTION_MAX_REFUSALS + 1):
            self.drive(_refusing(400))
        self.assertTrue(self.mgr.projection_failed(first.id))
        second = self.card("b")
        _, raised = self.drive(_refusing(400))
        self.assertFalse(raised.endpoint_wide, "the grace runs from the first refusal since an accept")
        self.assertEqual(self.mgr.store.projection(second.id).get("refusals"), 1)


def _cards_refused(notice_send):
    """Refuse every card with a 400; hand every notice to `notice_send`."""
    def send(payload):
        if WIRE_FIELD in payload["extra_content"]:
            _refusing(400)(payload)
        return notice_send(payload)
    return send


class NoticesOnARefusedPassTests(_RefusalDrive):
    """A pass that accepts no card still sends its due notices before re-raising the refusal,
    and a card given up on is announced to the owner exactly once per revision."""

    def setUp(self):
        super().setUp()
        self.sent = RecordingSender()

    def blocked_with_a_reminder_due(self):
        a = self.card("a")
        self.drive(self.sent)
        self.sent.sent.clear()
        self.card("b")
        return a

    def test_due_notices_go_out_on_a_pass_that_accepts_no_card(self):
        a = self.blocked_with_a_reminder_due()
        got, raised = self.drive(_cards_refused(self.sent))
        self.assertIsNotNone(raised, "the card refusal still reaches the caller")
        self.assertEqual(raised.code, 400)
        self.assertEqual([p["extra_content"][NOTICE_FIELD]["notice"] for p in self.sent.sent], ["reminder"])
        self.assertEqual([i for i, _ in got.done], [a.id])
        self.assertEqual(self.mgr.store.projection(a.id).get("reminders"), 1)

    def test_a_notice_failure_on_a_no_accept_pass_still_raises_the_card_refusal(self):
        self.blocked_with_a_reminder_due()

        def notice_down(payload):
            raise RuntimeError("notice transport down")
        got, raised = self.drive(_cards_refused(notice_down))
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, 400, "the notice failure never masks the card refusal")
        self.assertIs(raised.projection, got)

    def test_a_failed_card_is_announced_once_per_revision_across_a_restart(self):
        req = self.mgr.create(make_req(kind="choice", guard="q", message="Pick a deploy target\nmore",
                                       device={"id": "dq", "name": "n"},
                                       actions=[Action(id="ok", kind="choose", label="OK")]))
        self.drive(_refusing(400))  # opens the grace
        for _ in range(self.pj.PROJECTION_MAX_REFUSALS - 1):
            self.drive(_refusing(400))
        self.assertFalse(self.mgr.projection_failed(req.id))
        got, raised = self.drive(_cards_refused(self.sent))  # the bound: failed, then announced
        self.assertIsNotNone(raised)
        self.assertEqual([i for i, _, _ in got.failed], [req.id])
        self.assertEqual(len(self.sent.sent), 1)
        notice = self.sent.sent[0]
        self.assertEqual(notice["extra_content"][NOTICE_FIELD]["notice"], "failed")
        self.assertEqual(notice["dedupe_key"], f"hitl:{req.id}:failed:{req.revision}")
        self.assertIn("Pick a deploy target", notice["body"])
        self.assertNotIn(WIRE_FIELD, notice["extra_content"])
        for word in ("claim", "token", "lease", "guard"):
            self.assertNotIn(word, json.dumps(notice).lower())
        self.assertEqual(self.mgr.store.projection(req.id).get("failed_notice"), req.revision)
        self.pj._RETRY_DEADLINES.clear()
        self.pj._NO_ACCEPT_SINCE.clear()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))  # a fresh process over the same store
        self.assertEqual(pending_ids(self.mgr), [])
        got, raised = self.drive(self.sent)
        self.assertIsNone(raised)
        self.assertEqual((got.done, len(self.sent.sent)), ([], 1), "no second notice at the same revision")


_SAFE_INT = 2 ** 53 - 1  # canonical JSON's integer range is [-(2**53)+1, 2**53-1]


def _floats_at(o, path="$"):
    """Every path holding a number canonical JSON refuses: any float, or an int out of range."""
    if isinstance(o, float):
        return [path]
    if isinstance(o, int) and not isinstance(o, bool) and not -_SAFE_INT <= o <= _SAFE_INT:
        return [path]
    if isinstance(o, dict):
        return [f for k, v in o.items() for f in _floats_at(v, f"{path}.{k}")]
    if isinstance(o, (list, tuple)):
        return [f for i, v in enumerate(o) for f in _floats_at(v, f"{path}[{i}]")]
    return []


class NoFloatOnTheWireTests(unittest.TestCase):
    """A room refuses a float anywhere in event content (canonical JSON), so no projected
    payload, card or notice, carries one at any depth."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))

    def test_the_whole_projected_payload_has_no_float(self):
        req = self.mgr.create(make_req(
            kind="permission", expires_at=1793110319.208562,
            subject={"tool": "Bash", "input": {"timeout": 1.5, "retries": 3.0, "argv": [0.25, "x"],
                                               "nested": {"deep": [{"at": 1793110319.5}]}},
                     "options": [{"weight": 2.0}], "choices": {"a": "do a"}}))
        sent = RecordingSender()
        project(self.mgr, sent, ROOM)
        self.mgr.resolve(req.id)
        project(self.mgr, sent, ROOM)
        self.assertEqual(len(sent.sent), 2)
        for payload in sent.sent:
            self.assertEqual(_floats_at(payload), [], payload)
        wire = sent.sent[0]["extra_content"][WIRE_FIELD]
        self.assertEqual(wire["expires_at"], "1793110319.208562")
        self.assertEqual(wire["subject"]["input"],
                         {"timeout": "1.5", "retries": 3, "argv": ["0.25", "x"],
                          "nested": {"deep": [{"at": "1793110319.5"}]}})
        self.assertEqual(wire["subject"]["options"], [{"weight": 2}])

    def test_an_integral_expiry_stays_a_number(self):
        self.mgr.create(make_req(expires_at=1793110320.0))
        sent = RecordingSender()
        project(self.mgr, sent, ROOM)
        expires = sent.sent[0]["extra_content"][WIRE_FIELD]["expires_at"]
        self.assertEqual((type(expires), expires), (int, 1793110320))

    def test_numbers_at_the_edge_of_the_canonical_range_through_the_persisted_store(self):
        edge = {"max": _SAFE_INT, "min": -_SAFE_INT, "over": 2 ** 53, "under": -(2 ** 53),
                "max_f": float(_SAFE_INT), "min_f": float(-_SAFE_INT),
                "over_f": float(2 ** 53), "under_f": -float(2 ** 53),
                "huge_f": 1e100, "huge": 10 ** 30, "flag": True}
        req = self.mgr.create(make_req(kind="permission", expires_at=float(2 ** 53),
                                       subject={"tool": "Bash", "input": edge}))
        self.assertEqual(HitlManager(HitlStore(Path(self.tmp.name))).get(req.id).subject["input"], edge,
                         "the store kept every value as written")
        sent = RecordingSender()
        project(self.mgr, sent, ROOM)
        self.assertEqual(_floats_at(sent.sent[0]), [], sent.sent[0])
        wire = sent.sent[0]["extra_content"][WIRE_FIELD]
        self.assertEqual(wire["subject"]["input"], {
            "max": _SAFE_INT, "min": -_SAFE_INT, "over": "9007199254740992", "under": "-9007199254740992",
            "max_f": _SAFE_INT, "min_f": -_SAFE_INT,
            "over_f": "9007199254740992.0", "under_f": "-9007199254740992.0",
            "huge_f": "1e+100", "huge": str(10 ** 30), "flag": True})
        self.assertEqual(wire["expires_at"], "9007199254740992.0")

    def test_a_due_reminder_notice_carries_no_float(self):
        """The notice is built from the persisted device, not from to_wire(): its own guard is pinned."""
        self.mgr.create(make_req(device={"id": 7.5, "name": "n", "host": 2.5},
                                 created_at=1000.0, updated_at=1000.0))
        sent = RecordingSender()
        project(self.mgr, sent, ROOM, clock=lambda: 1000.0)
        project(self.mgr, sent, ROOM, clock=lambda: 1000.0 + 31 * 60)
        notices = [p for p in sent.sent if WIRE_FIELD not in p["extra_content"]]
        self.assertEqual(len(notices), 1, "a reminder notice went out")
        for payload in sent.sent:
            self.assertEqual(_floats_at(payload), [], payload)


if __name__ == "__main__":
    unittest.main()

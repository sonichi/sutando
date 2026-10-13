"""Projector contract: CREATE then EDIT, idempotent on the revision ledger,
retry-whole on a rejected send, dedupe keys stable per (requirement, revision).
"""

import io
import subprocess
import urllib.error
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
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
        with self.assertLogs("hitl.projector", level="WARNING"):
            self.assertEqual(pending_ids(self.mgr), [card.id])
        self.assertEqual(self.live(), sorted([settled.id, card.id]))

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



def http_error(code, body=b""):
    return urllib.error.HTTPError("https://relay.invalid/v1/room", code, "refused", {},
                                  io.BytesIO(body))


def read_body(e):
    return e.read().decode() if hasattr(e, "read") else ""


class PerCardIsolationTests(unittest.TestCase):
    """One card whose send raises must not abort the pass for every later card."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def _drive(self, send, now=None):
        clock = (lambda: now) if now is not None else time.time
        return project(self.mgr, send, ROOM, clock=clock)

    def test_a_poison_card_that_sorts_first_does_not_block_later_cards_or_notices(self):
        due = self.mgr.create(make_req(guard="due", device={"id": "d-due"}))
        project(self.mgr, RecordingSender(), ROOM, clock=lambda: 1000.0)
        a = self.mgr.create(make_req(kind="choice", guard="a", device={"id": "d-a"}))
        b = self.mgr.create(make_req(kind="choice", guard="b", device={"id": "d-b"}))
        poison, later = sorted([a.id, b.id])
        self.assertEqual([r.id for r in self.mgr.store.all()
                          if self.mgr.needs_projection(r.id)][0], poison)
        good = RecordingSender()

        def send(payload):
            if payload["dedupe_key"].startswith(f"hitl:{poison}:"):
                raise http_error(400, b'{"error": "bad edit"}')
            return good(payload)

        done = self._drive(send, now=1000.0 + 31 * 60)
        self.assertIn(later, [rid for rid, _ in done], "the card after the poison was not sent")
        self.assertTrue(any(NOTICE_FIELD in p["extra_content"] for p in good.sent),
                        "the reminder notice never went out")
        self.assertTrue(self.mgr.needs_projection(poison), "a non-terminal card stays retryable")

    def test_a_notice_build_failure_is_logged_and_does_not_block_a_later_notice(self):
        # A bug building the notice body/dedupe_key must be logged, not
        # swallowed, and must not stop a later card's own due notice.
        bad = self.mgr.create(make_req(guard="bad", device={"id": "d-bad"}))
        good = self.mgr.create(make_req(guard="good", device={"id": "d-good"}))
        project(self.mgr, RecordingSender(), ROOM, clock=lambda: 1000.0)
        import hitl.renotify as renotify_mod
        real_body = renotify_mod.body

        def flaky_body(notice, req, now, **kw):
            if req.id == bad.id:
                raise RuntimeError("bad renotify body")
            return real_body(notice, req, now, **kw)

        logged = []
        sender = RecordingSender()
        with mock.patch.object(renotify_mod, "body", side_effect=flaky_body):
            project(self.mgr, sender, ROOM, clock=lambda: 1000.0 + 31 * 60, log=logged.append)
        self.assertTrue(any(bad.id in line and "bad renotify body" in line for line in logged), logged)
        self.assertTrue(any(NOTICE_FIELD in p["extra_content"] for p in sender.sent),
                        "the good card's own notice was blocked by the bad one")

    def test_a_failed_send_is_logged_with_its_card_id_and_response_body(self):
        req = self.mgr.create(make_req(kind="choice"))
        ok = self.mgr.create(make_req(kind="choice", guard="g2"))
        logged = []

        def send(payload):
            if payload["dedupe_key"].startswith(f"hitl:{req.id}:"):
                raise http_error(400, b'{"error": "bad edit"}')
            return RecordingSender()(payload)

        done = project(self.mgr, send, ROOM, log=logged.append, error_body=read_body)
        self.assertEqual([rid for rid, _ in done], [ok.id])
        self.assertTrue(any(req.id in line and "bad edit" in line for line in logged), logged)

    def test_a_long_error_body_is_truncated_to_200_chars_in_the_log(self):
        req = self.mgr.create(make_req(kind="choice"))
        long_body = ("x" * 500).encode()
        logged = []

        def send(payload):
            raise http_error(400, long_body)

        with self.assertRaises(urllib.error.HTTPError):
            project(self.mgr, send, ROOM, log=logged.append, error_body=read_body)
        line = next(l for l in logged if req.id in l)
        self.assertLessEqual(line.count("x"), 200, line)

    def _resolved_card(self, guard="g1"):
        req = self.mgr.create(make_req(kind="choice", guard=guard))
        project(self.mgr, RecordingSender(), ROOM)
        self.mgr.resolve(req.id)
        return req

    def test_a_terminal_card_whose_edit_gets_a_4xx_stops_being_retried(self):
        req = self._resolved_card()
        attempts = []

        def send(payload):
            attempts.append(payload)
            raise http_error(400, b"already resolved")

        with self.assertRaises(urllib.error.HTTPError):
            self._drive(send)
        self.assertFalse(self.mgr.needs_projection(req.id))
        self.assertEqual(self._drive(send), [])
        self.assertEqual(len(attempts), 1, "the refused edit was retried")
        self.assertEqual(pending_ids(self.mgr), [], "the settled card must not stay pending")

    def test_a_5xx_on_a_terminal_card_is_retried(self):
        req = self._resolved_card()

        def send(payload):
            raise http_error(503, b"unavailable")

        with self.assertRaises(urllib.error.HTTPError):
            self._drive(send)
        self.assertTrue(self.mgr.needs_projection(req.id), "a 5xx must stay retryable")
        ok = RecordingSender()
        self._drive(ok)
        self.assertEqual([p["op"] for p in ok.sent], ["edit"])
        self.assertFalse(self.mgr.needs_projection(req.id))

    def test_auth_timeout_and_ratelimit_on_a_terminal_card_stay_retryable(self):
        # 401/403/408/429 are blips, not refusals that will never change --
        # settling on them would archive a card whose edit never landed.
        for code in (401, 403, 408, 429):
            req = self._resolved_card(guard=f"g-{code}")

            def send(payload, _code=code):
                raise http_error(_code, b"blip")

            with self.assertRaises(urllib.error.HTTPError):
                self._drive(send)
            self.assertTrue(self.mgr.needs_projection(req.id), code)
            ok = RecordingSender()
            self._drive(ok)
            self.assertEqual([p["op"] for p in ok.sent], ["edit"], code)
            self.assertFalse(self.mgr.needs_projection(req.id), code)

    def test_a_pass_where_every_send_fails_still_raises(self):
        self.mgr.create(make_req(kind="choice", guard="a"))
        self.mgr.create(make_req(kind="choice", guard="b"))
        calls = []

        def send(payload):
            calls.append(payload)
            raise RuntimeError("relay down")

        with self.assertRaises(RuntimeError):
            self._drive(send)
        self.assertEqual(len(calls), 2, "every card is still attempted before the pass raises")

if __name__ == "__main__":
    unittest.main()

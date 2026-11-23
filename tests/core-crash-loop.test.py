#!/usr/bin/env python3
"""A crash loop reaches the owner as one card per incident (P0-6).

Before: the backend supervisor's spent recovery budget (`terminal` in supervisor-status.json)
and a core that keeps dying never left the host. Now the core's monitor raises one core-blocked
card per incident: the service and its last error, or, when the recovery record says
`crash_loop` for the core (Claude Code or Codex), the restart count and Restart core. A stale
status file, another app session's terminal, or a core that is up again raises nothing and
closes what is open.

Dead ends closed: a service the supervisor marks `resettable` gets a Start <service> again button
whose tap hands the supervisor one request (state/control/services/) and whose card closes once the
service is no longer stopped; a core the app's revive loop gave up on (state/core-revive.json) is
`crash_loop` with `reason: revive_exhausted` in the record and a fresh "Can't recover" card.
"""
import importlib.util as u
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_crash", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

import core_crash_loop as C
import core_recovery_state as crs
from hitl.manager import HitlManager, HitlStore
from hitl.replies import REPLY_FIELD
from hitl.schema import STATUS_IN_PROGRESS, STATUS_RESOLVED

CORE = "sutando-core"
RUN = "run-3f6b1c2a"
OWNER = "@owner:s.space"
DM = "!dm:s.space"


INCIDENT = "st_1a2b3c4d"


def status(now, terminal=True, session=RUN, beat_age=2.0, error="exited (code 1): TypeError: x is undefined",
           resettable=False, incident=INCIDENT, app_session=None, verdict="failed", trip_age=30.0):
    """supervisor-status.json: `session` is the terminal marker's, `app_session` the running app's."""
    svc = {"recoveryBudget": {"used": 5, "cap": 5, "windowEndsAt": None},
           "lifecycle": {"verdict": verdict, "reason": error}}
    if terminal:
        svc["terminal"] = {"state": "failed", "reason": "crash-loop", "appSessionId": session}
        if resettable:
            svc["terminal"].update(resettable=True, incident=incident, at=int((now - trip_age) * 1000))
    return {"schemaVersion": 1, "heartbeatAt": int((now - beat_age) * 1000), "appSessionId": app_session or RUN,
            "services": {"voice-agent": svc, "agent-api": {"recoveryBudget": {"used": 0, "cap": 5}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "state").mkdir()
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc):
        (self.ws / "state" / C.STATUS_FILE).write_text(json.dumps(doc))

    def tick(self, now=None, runtime="claude", pane=None):
        return M.crash_loop_step(self.mgr, self.ws, CORE, runtime, pane, time.time() if now is None else now)


class SupervisorTerminal(Base):
    def test_a_spent_budget_raises_one_card_with_the_service_and_its_last_error(self):
        now = time.time()
        self.write(status(now))
        [a] = self.tick(now=now)
        [b] = self.tick(now=now + 3)
        self.assertEqual(a.id, b.id, "one card per incident")
        self.assertEqual(a.kind, "core-blocked")
        self.assertEqual(a.subject["crash_loop"], "voice-agent")
        self.assertIn("voice-agent keeps crashing", a.message)
        self.assertIn("voice-agent keeps crashing, so the app stopped restarting it (crash-loop).", a.message)
        self.assertIn("Last error: exited with an error", a.message, "named by its kind, never quoted")
        self.assertNotIn("TypeError", a.message)
        self.assertIn("until the s.space app restarts", a.message)
        self.assertEqual(a.actions, [], "no button: nothing on the card can clear a terminal service")

    def test_a_secret_or_a_path_in_the_supervisors_error_never_reaches_the_card(self):
        now = time.time()
        for error, kind in (("spawn /Users/someone/app/bin/node ENOENT token=sk-ant-oat01-SECRETSECRET",
                             "missing file or module"),
                            ("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789", "not recorded"),
                            ("JavaScript heap out of memory at /home/x/y.js", "out of memory")):
            with self.subTest(kind=kind):
                self.write(status(now, error=error))
                [card] = self.tick(now=now)
                self.assertIn(f"Last error: {kind}", card.message)
                for leak in ("/Users/someone", "sk-ant", "ghp_", "/home/x", "Bearer"):
                    self.assertNotIn(leak, card.message)
                self.mgr.resolve(card.id)
        doc = status(now)
        doc["services"]["voice-agent"]["terminal"]["reason"] = "gave up: /Users/someone/x"
        doc["services"]["../etc"] = doc["services"].pop("voice-agent")
        self.write(doc)
        [card] = self.tick(now=now + 1)
        self.assertNotIn("/Users/someone", card.message)
        self.assertIn("a service keeps crashing", card.message)

    def test_stale_status_or_another_session_raises_nothing(self):
        now = time.time()
        self.write(status(now, beat_age=C.STATUS_FRESH_S + 5))
        self.assertEqual(self.tick(now=now), [])
        self.write(status(now, session="run-older"))
        self.assertEqual(self.tick(now=now), [])
        (self.ws / "state" / C.STATUS_FILE).write_text("{not json")
        self.assertEqual(self.tick(now=now), [])

    def test_a_fresh_app_session_closes_the_card(self):
        now = time.time()
        self.write(status(now))
        [a] = self.tick(now=now)
        self.write(status(now + 10, terminal=False))
        self.assertEqual(self.tick(now=now + 10), [])
        self.assertEqual(self.mgr.get(a.id).status, STATUS_RESOLVED)


class CoreCrashes(Base):
    def record(self, runtime="claude", restarts=5, sup="crashed", now=None):
        now = time.time() if now is None else now
        rec = crs.derive({"state": sup}, None, None, None, None, now=now, runtime=runtime,
                         restarts_10m=restarts, prev=crs.read(self.ws, now, max_age_s=None))
        crs.publish(self.ws, rec, now)
        return rec

    def test_the_recovery_records_crash_loop_raises_one_card_with_restart_core(self):
        self.assertEqual(self.record(restarts=crs.CRASH_LOOP_RESTARTS - 1)["state"], "starting")
        self.assertEqual(self.tick(), [], "a few restarts are the supervisor's job, not the owner's")
        rec = self.record()
        [card] = self.tick()
        self.assertEqual([a.id for a in card.actions], ["restart_core"])
        self.assertEqual(card.subject["incident"], rec["incident"], "one incident, the record's")
        self.assertIn("started 5 times in the last 10 minutes", card.message)
        self.record(restarts=6)
        [again] = self.tick()
        self.assertEqual(again.id, card.id, "the loop going on is the same card")

    def test_a_codex_core_crash_loop_is_carded_the_same_way(self):
        self.record(runtime="codex")
        [card] = self.tick(runtime="codex")
        self.assertEqual((card.runtime, [a.id for a in card.actions]), ("codex", ["restart_core"]))

    def test_the_last_error_is_named_never_quoted(self):
        self.record()
        [card] = self.tick(pane="API Error: 500 {\"error\":\"overloaded\"} token=sk-ant-oat01-SECRET")
        self.assertIn("Last error: api error", card.message)
        self.assertNotIn("sk-ant", card.message)

    def test_the_card_closes_once_the_core_is_up(self):
        self.record()
        [card] = self.tick()
        self.record(sup="running", restarts=0)
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.mgr.get(card.id).status, STATUS_RESOLVED)

    def test_a_seat_monitor_never_raises_it(self):
        # Behaviour, not source text: one monitor tick (--once) of a seat on a host whose recovery
        # record says crash_loop calls no crash-loop step; the core's tick does.
        import os
        from unittest import mock
        self.record()
        calls = []

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "ok"}

        def tick(*extra):
            argv = ["core-input-watch.py", "--socket", "/nonexistent.sock", "--once",
                    "--out", str(self.ws / "state" / "core-supervisor.json"), *extra]
            with mock.patch.object(M, "crash_loop_step", lambda *a, **k: calls.append(a[2]) or []), \
                    mock.patch.object(M, "capture_styled", lambda *a: "❯ \n"), \
                    mock.patch.object(M, "_load_runtime_health", lambda: _RH()), \
                    mock.patch.object(M, "gateway_alive", lambda *a: True), \
                    mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                    mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                    mock.patch.object(M, "send_keys", lambda *a: False), \
                    mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.ws / "cfg")}), \
                    mock.patch.object(sys, "argv", argv):
                M.main()
        tick("--session", "sutando-worker-b35461a4", "--seat", "worker Mira (b35461a4)")
        self.assertEqual(calls, [], "a seat's monitor ran the crash-loop step")
        tick("--session", CORE)
        self.assertEqual(calls, [CORE], "the core's monitor runs it")

class ResetService(Base):
    """A crash-looped service the supervisor can retry gets one button. The tap is bound to the
    card's own app session and breaker trip, and its outcome is the supervisor's own answer."""

    def setUp(self):
        super().setUp()
        import recovery_actions as RA
        from hitl import recovery
        from hitl.replies import HitlReplyHandler
        self.RA, self.recovery = RA, recovery
        self.handler = HitlReplyHandler(self.mgr, OWNER, workspace=self.ws, log=lambda m: None)

    def folder(self):
        return self.ws.joinpath(*C.RESET_DIR)

    def requests(self):
        return [json.loads(p.read_text()) for p in sorted(self.folder().glob("req-*.json"))] \
            if self.folder().is_dir() else []

    def answer(self, req, outcome="running", at=None, **over):
        """The supervisor's result for `req`, as engine/service-reset.mjs writes it."""
        body = {"schemaVersion": 1, "id": req["id"], "kind": "reset-service-result", "service": req["service"],
                "appSessionId": req["appSessionId"], "incident": req["incident"], "outcome": outcome,
                "atMs": req["requestedAtMs"] + 3000 if at is None else int(at * 1000), **over}
        (self.folder() / f"res-{req['id']}.json").write_text(json.dumps(body))

    def tap(self, card, aid="reset_service:voice-agent"):
        if not self.mgr.store.projection(card.id).get("event_id"):
            self.mgr.record_projection(card.id, card.revision, f"$card-{card.id}", {"room_id": DM})
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": OWNER, "room_id": DM,
                            "content": {REPLY_FIELD: {"hitl_id": card.id, "expected_revision": card.revision,
                                                      "action_id": aid, "guard": card.guard}}})
        return self.handler.last_outcome

    def run_pending(self, now):
        target = self.RA.Target(self.ws, "/tmp/x.sock", CORE)
        return self.RA.run_pending(self.mgr, target, runner=lambda *a, **k: None, now=now)

    def tapped(self, now):
        """A resettable card, tapped and executed: (card, the request the supervisor would read)."""
        self.write(status(now, resettable=True))
        [card] = self.tick(now=now)
        self.assertEqual(self.tap(card), "applied")
        [receipt] = self.run_pending(now + 1)
        self.assertEqual(receipt["outcome"], "done")
        [req] = self.requests()
        return card, req

    def receipt(self, card):
        return self.recovery.read_receipt(self.ws, card.id)

    def test_a_resettable_service_card_carries_one_start_again_button(self):
        now = time.time()
        self.write(status(now, resettable=True))
        [card] = self.tick(now=now)
        self.assertEqual([(a.id, a.label) for a in card.actions],
                         [("reset_service:voice-agent", "Start voice-agent again")])
        self.assertTrue(card.subject["recovery"])
        self.assertEqual((card.subject["service_session"], card.subject["service_incident"]), (RUN, INCIDENT))
        self.assertIn("Tap Start voice-agent again", card.message)
        self.assertNotIn("until the s.space app restarts", card.message)

    def test_the_request_carries_the_cards_own_session_and_incident(self):
        now = time.time()
        card, req = self.tapped(now)
        self.assertEqual((req["schemaVersion"], req["kind"], req["service"]), (1, "reset-service", "voice-agent"))
        self.assertEqual((req["appSessionId"], req["incident"]), (RUN, INCIDENT), "the card's, not whatever is current")
        self.assertEqual(req["requestedAtMs"], int((now + 1) * 1000))
        open_card = self.mgr.get(card.id)
        self.assertIn("Asked the app to start voice-agent again", open_card.message)
        self.assertNotIn("receipt", open_card.subject, "no outcome is claimed before the supervisor answers")

    def test_a_vanished_stop_marker_is_not_proof_the_service_runs(self):
        now = time.time()
        card, req = self.tapped(now)
        self.write(status(now + 5, terminal=False))
        self.assertEqual(self.tick(now=now + 5), [])
        still = self.mgr.get(card.id)
        self.assertEqual(still.status, STATUS_IN_PROGRESS, "the marker going away closes nothing")
        self.assertNotIn("running again", still.message)
        self.assertEqual(self.RA.verify_step(self.mgr, self.ws, CORE, blocked=False, now=now + 90), [],
                         "the core being up says nothing about the service")
        self.answer(req)
        self.tick(now=now + 6)
        done = self.mgr.get(card.id)
        self.assertEqual(done.status, STATUS_RESOLVED)
        self.assertIn("voice-agent is running again.", done.message)
        self.assertEqual(done.subject["receipt"]["outcome"], "done")
        self.assertEqual(self.receipt(card)["outcome"], "done")

    def test_a_failed_start_is_said_and_recorded_as_failed(self):
        now = time.time()
        card, req = self.tapped(now)
        self.answer(req, outcome="failed")
        self.tick(now=now + 6)
        closed = self.mgr.get(card.id)
        self.assertEqual(closed.status, STATUS_RESOLVED)
        self.assertIn("voice-agent did not start", closed.message)
        self.assertEqual(closed.subject["receipt"]["outcome"], "failed")
        self.assertEqual(self.receipt(card)["outcome"], "failed", "the receipt on disk is corrected")
        audit = (self.recovery.recovery_dir(self.ws) / self.RA.AUDIT_FILE).read_text().splitlines()
        self.assertEqual([json.loads(x)["outcome"] for x in audit], ["done", "failed"])

    def test_no_answer_is_a_timeout_never_a_success(self):
        now = time.time()
        card, req = self.tapped(now)
        self.write(status(now + 5, terminal=False))
        self.tick(now=now + C.RESET_TIMEOUT_S - 5)
        self.assertEqual(self.mgr.get(card.id).status, STATUS_IN_PROGRESS, "still inside the wait")
        self.tick(now=now + C.RESET_TIMEOUT_S + 5)
        closed = self.mgr.get(card.id)
        self.assertEqual(closed.status, STATUS_RESOLVED)
        self.assertIn("did not answer", closed.message)
        self.assertEqual(self.receipt(card)["outcome"], "failed")

    def test_an_answer_for_another_request_session_or_incident_or_a_stale_one_is_ignored(self):
        now = time.time()
        card, req = self.tapped(now)
        for over in ({"id": "f" * 32}, {"appSessionId": "run-other"}, {"incident": "st_ffffffff"},
                     {"service": "agent-api"}, {"atMs": req["requestedAtMs"] - 60_000}, {"atMs": "soon"},
                     {"schemaVersion": 2}, {"outcome": 7}):
            with self.subTest(over=over):
                self.answer(req, **over)
                self.tick(now=now + 6)
                self.assertEqual(self.mgr.get(card.id).status, STATUS_IN_PROGRESS, over)
        (self.folder() / f"res-{req['id']}.json").write_text("[not an object")
        self.tick(now=now + 7)
        self.assertEqual(self.mgr.get(card.id).status, STATUS_IN_PROGRESS)

    def test_session_turnover_makes_the_old_cards_tap_stale(self):
        now = time.time()
        self.write(status(now, resettable=True))
        [card] = self.tick(now=now)
        self.assertEqual(self.tap(card), "applied")
        # The app restarted and the service crash-looped again: another session, another trip.
        self.write(status(now + 1, resettable=True, session="run-new", app_session="run-new", incident="st_99999999"))
        [receipt] = self.run_pending(now + 2)
        self.assertEqual(receipt["outcome"], "stale")
        self.assertEqual(self.requests(), [], "nothing is asked of a session the card was not raised in")
        self.assertIn("no longer stopped by the crash this card is about", self.mgr.get(card.id).message)

    def test_a_newer_incident_in_the_same_session_makes_the_old_cards_tap_stale(self):
        now = time.time()
        self.write(status(now, resettable=True))
        [card] = self.tick(now=now)
        self.tap(card)
        self.write(status(now + 1, resettable=True, incident="st_99999999"))
        [receipt] = self.run_pending(now + 2)
        self.assertEqual(receipt["outcome"], "stale")
        self.assertEqual(self.requests(), [])
        [fresh] = self.tick(now=now + 3)
        self.assertNotEqual(fresh.id, card.id, "the newer trip has its own card")
        self.assertEqual(fresh.subject["service_incident"], "st_99999999")

    def test_a_tap_after_the_service_stopped_being_stopped_writes_nothing(self):
        now = time.time()
        self.write(status(now, resettable=True))
        [card] = self.tick(now=now)
        self.tap(card)
        self.write(status(now, terminal=False))
        [receipt] = self.run_pending(now + 1)
        self.assertEqual(receipt["outcome"], "stale")
        self.assertEqual(self.requests(), [])

    def test_a_request_names_exactly_the_stop_the_status_reports(self):
        now = time.time()
        self.write(status(now))
        self.assertIsNone(C.request_reset(self.ws, "voice-agent", RUN, INCIDENT, now), "no marker identity")
        self.write(status(now, resettable=True))
        for service, session, incident in (("agent-api", RUN, INCIDENT), ("voice-agent", "run-other", INCIDENT),
                                           ("voice-agent", RUN, "st_00000000"), ("voice-agent", RUN, "")):
            self.assertIsNone(C.request_reset(self.ws, service, session, incident, now), (service, session, incident))
        self.assertTrue(C.request_reset(self.ws, "voice-agent", RUN, INCIDENT, now))
        self.assertEqual(len(self.requests()), 1)

    def test_a_stop_marker_is_checked_for_identity_and_freshness_before_it_raises_a_card(self):
        now = time.time()
        for why, doc in (("stamped after the heartbeat that carries it", status(now, resettable=True, trip_age=-600)),
                         ("the supervisor itself says it is running", status(now, resettable=True, verdict="running")),
                         ("adopted", status(now, resettable=True, verdict="adopted")),
                         ("another session's marker", status(now, resettable=True, session="run-older"))):
            with self.subTest(why=why):
                self.write(doc)
                self.assertEqual(C.terminal_services(self.ws, now), [], why)
                self.assertEqual(self.tick(now=now), [], why)
        for why, edit in (("no incident", {"incident": None}), ("an incident that is not an id", {"incident": "../x"}),
                          ("no trip time", {"at": None}), ("a trip time that is not a number", {"at": "now"})):
            with self.subTest(why=why):
                doc = status(now, resettable=True)
                doc["services"]["voice-agent"]["terminal"].update(edit)
                self.write(doc)
                found = C.terminal_services(self.ws, now)
                self.assertEqual([s["resettable"] for s in found] if edit.get("at") != "now" else found,
                                 [False] if edit.get("at") != "now" else [], why)

    def test_the_id_names_one_service_shaped_word(self):
        self.assertEqual(crs.parse_action_id("reset_service:voice-agent", "codex"), ("reset_service", "voice-agent"))
        self.assertEqual(crs.parse_action_id("reset_service:agent-api", "agy"), ("reset_service", "agent-api"))
        for bad in ("reset_service", "reset_service:", "reset_service:../etc", "reset_service:a b",
                    "reset_service:x\n", "reset_service:" + "a" * 41, "reset_service:-x"):
            self.assertIsNone(crs.parse_action_id(bad), repr(bad))
        self.assertIn(self.recovery.RESET_SERVICE, self.recovery.CORE_ONLY, "a seat's card never carries it")


class ReviveExhausted(Base):
    """The app's revive loop giving up reaches the record and the owner's card."""

    def give_up(self, on=True):
        path = self.ws / "state" / crs.REVIVE_FILE
        if on:
            path.write_text(json.dumps({"v": 1, "state": "exhausted", "since": 1763730000, "attempts": 25}))
        elif path.exists():
            path.unlink()

    def record(self, sup="crashed", restarts=1, now=None):
        now = time.time() if now is None else now
        rec = crs.gather(self.ws, {"state": sup}, now=now, runtime="claude")
        crs.publish(self.ws, rec, now)
        return rec

    def test_a_core_the_app_gave_up_on_is_a_crash_loop_with_its_reason(self):
        self.give_up()
        rec = self.record()
        self.assertEqual((rec["state"], rec["reason"]), ("crash_loop", crs.REVIVE_EXHAUSTED))
        self.assertEqual(rec["actions"], ["restart_core"])
        self.assertEqual(crs.coerce(rec), rec, "the reason survives the record's own bounds")
        self.assertEqual(self.record(sup=None)["state"], "crash_loop", "a core no state names is not up")

    def test_a_core_that_is_up_or_no_give_up_says_nothing_of_it(self):
        self.give_up()
        self.assertNotIn("reason", self.record(sup="running"))
        self.give_up(False)
        rec = self.record()
        self.assertEqual(rec["state"], "starting")
        self.assertNotIn("reason", rec)
        (self.ws / "state" / crs.REVIVE_FILE).write_text(json.dumps({"v": 1, "state": "ok"}))
        self.assertFalse(crs.revive_exhausted(self.ws))
        self.assertIsNone(crs.coerce({"v": 1, "state": "crash_loop", "reason": "nope"}).get("reason"))

    def test_giving_up_replaces_the_crash_card_with_a_cant_recover_card(self):
        now = time.time()
        crs.publish(self.ws, crs.derive({"state": "crashed"}, None, None, None, None, now=now,
                                        runtime="claude", restarts_10m=5), now)
        [crashing] = self.tick(now=now)
        self.assertEqual(crashing.title, "Crashing: core")
        self.give_up()
        rec = crs.derive({"state": "crashed"}, None, None, None, None, now=now + 1, runtime="claude",
                         restarts_10m=5, prev=crs.read(self.ws, now + 1, max_age_s=None), revive_exhausted=True)
        crs.publish(self.ws, rec, now + 1)
        [gave_up] = self.tick(now=now + 1)
        self.assertNotEqual(gave_up.id, crashing.id, "a new card, so the owner is told again")
        self.assertEqual(gave_up.title, "Can't recover: core")
        self.assertIn("could not revive it after repeated tries", gave_up.message)
        self.assertEqual([a.id for a in gave_up.actions], ["restart_core"])
        self.assertEqual(self.mgr.get(crashing.id).status, STATUS_RESOLVED)


if __name__ == "__main__":
    unittest.main()

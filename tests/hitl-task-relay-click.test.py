#!/usr/bin/env python3
"""remote-gateway-bridge: a card click on the TASK relay is applied to the HITL
store and consumed; ordinary messages, and clicks while the owner is unknown, stay on the task path."""
import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hitl-relay-click-"))
for p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if p not in sys.path:
        sys.path.insert(0, p)
from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=TMP / "tasks", result_dir=TMP / "results", state_dir=TMP / "state")
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

OWNER = "@owner:ag2.space"


class TaskRelayClickTests(unittest.TestCase):
    def setUp(self):
        os.environ["SPARROW_HA_OWNER"] = OWNER
        rgb._HITL_OWNER.update(mxid="", at=0.0, bad_at=None)
        self.mgr = HitlManager(HitlStore(default_store(TMP)))
        self.req = self.mgr.create(HumanRequirement(
            kind="permission", runtime="claude", message="Claude wants to run Bash: ls",
            guard=f"hook:{os.urandom(4).hex()}", device={"id": "core-1", "name": "core-1"},
            actions=[Action(id="allow", kind="allow_once", label="Allow"),
                     Action(id="deny", kind="reject_once", label="Deny")]))

    def _on_disk(self):
        """The requirement as a fresh manager reads it from the store file."""
        return HitlManager(HitlStore(default_store(TMP))).get(self.req.id)

    @contextlib.contextmanager
    def _registry(self, owner):
        """A gateway whose /v1/agents row for this agent names `owner`; yields the reads."""
        reads = []

        def req(method, path, *a, **k):
            reads.append(path)
            return {"agents": [{"id": "@me:ag2.space", "owner": owner}]}
        with mock.patch.object(rgb, "_req", req), \
                mock.patch.object(rgb, "_reenroll_identity", lambda: "@me:ag2.space"):
            yield reads

    def _task(self, **kw):
        t = {"id": f"task-{os.urandom(5).hex()}", "channel_id": "!dm:ag2.space",
             "user_id": OWNER, "source_message_id": "$click", "task": "Allow"}
        t.update(kw)
        return t

    def test_non_owner_typed_label_reply_stays_a_message(self):
        """A non-owner's typed "Deny" is a message and must reach the task path;
        a non-owner's real click is still consumed. Own card id: the store
        outlives one test and requirement_for_event returns the first match."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-nonowner")
        t = self._task(user_id="@someone-else:ag2.space", reply_to_event="$card-nonowner", task="Deny")
        self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        click = self._task(user_id="@someone-else:ag2.space",
                           hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "deny", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(click), "ignored")
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_hitl_action_on_the_task_is_applied_and_consumed(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "allow")

    def test_reply_to_the_card_with_a_label_is_applied(self):
        self.mgr.record_projection(self.req.id, self.req.revision, "$card")
        self.assertTrue(rgb._handle_hitl_action(self._task(reply_to_event="$card", task="Deny")))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")

    def test_ordinary_message_stays_on_the_task_path(self):
        self.assertFalse(rgb._handle_hitl_action(self._task(task="Allow")))
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_no_owner_anywhere_leaves_the_click_as_a_task_and_says_so(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []

        def offline(*a, **k):
            raise OSError("gateway unreachable")
        with mock.patch.object(rgb, "_req", offline), mock.patch.object(rgb, "_log", logs.append):
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self._on_disk().chosen_action)
        self.assertTrue(any("hitl: card click ignored, owner unknown" in m for m in logs), logs)

    def test_registry_with_no_owner_row_fails_closed_too(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self._on_disk().chosen_action)
        self.assertTrue(any("owner unknown" in m for m in logs), logs)

    def test_an_ordinary_reply_with_no_owner_logs_nothing(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        logs = []
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            self.assertIs(rgb._handle_hitl_action(self._task(reply_to_event="$not-a-card", task="hi")), False)
        self.assertFalse(any("owner unknown" in m for m in logs), logs)

    def test_unset_variable_uses_the_registry_owner(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        with self._registry(OWNER) as reads:
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(t), "applied")
            self.assertEqual(self._on_disk().chosen_action, "allow", "the store file records the click")
            rgb._handle_hitl_action(self._task(task="Allow"))
            rgb._handle_hitl_action(self._task(hitl_action=dict(t["hitl_action"])))
        self.assertEqual(len(reads), 1, "the registry is read once within the TTL")

    def test_registry_owner_does_not_let_a_non_owner_resolve(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        with self._registry(OWNER):
            click = self._task(user_id="@someone-else:ag2.space",
                               hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                            "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(click), "ignored")
        self.assertIsNone(self._on_disk().chosen_action)

    def test_explicit_variable_wins_over_the_registry(self):
        os.environ["SPARROW_HA_OWNER"] = "@configured:ag2.space"
        with self._registry(OWNER) as reads:
            t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "allow", "guard": self.req.guard})
            self.assertEqual(rgb._handle_hitl_action(t), "ignored", "the registry owner is not the configured one")
            t2 = self._task(user_id="@configured:ag2.space", hitl_action=dict(t["hitl_action"]))
            self.assertEqual(rgb._handle_hitl_action(t2), "applied")
        self.assertEqual(reads, [], "an explicit owner never reads the registry")

    def test_event_channel_claimant_uses_the_registry_owner(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        claimant = rgb._HitlClicks(rgb._hitl_reply_handler("", log=lambda m: None), log=lambda m: None)

        def ev(actor):
            return {"type": "message.created", "event_id": f"$e{os.urandom(3).hex()}", "room_id": "!dm:ag2.space",
                    "actor_id": actor, "content": {"space.ag2.hitl.action": {
                        "hitl_id": self.req.id, "expected_revision": self.req.revision,
                        "action_id": "allow", "guard": self.req.guard}}}
        logs = []
        claimant._log = logs.append
        with self._registry(""), mock.patch.object(rgb, "_log", logs.append):
            self.assertFalse(claimant.claims(ev(OWNER)), "unknown owner: not claimed")
        self.assertTrue(any("owner unknown" in m for m in logs), logs)
        rgb._HITL_OWNER.update(mxid="", at=0.0, bad_at=None)
        with self._registry(OWNER):
            self.assertTrue(claimant.claims(ev("@someone-else:ag2.space")))
            claimant.offer(ev("@someone-else:ag2.space"))
            self.assertIsNone(self._on_disk().chosen_action, "a non-owner never resolves")
            e = ev(OWNER)
            self.assertTrue(claimant.claims(e))
            claimant.offer(e)
        self.assertEqual(self._on_disk().chosen_action, "allow")

    # -- owner lookup cost and freshness ------------------------------------------

    def _new_req(self):
        req = self.mgr.create(HumanRequirement(
            kind="permission", runtime="claude", message="Claude wants to run Bash: ls",
            guard=f"hook:{os.urandom(4).hex()}", device={"id": "core-1", "name": "core-1"},
            actions=[Action(id="allow", kind="allow_once", label="Allow"),
                     Action(id="deny", kind="reject_once", label="Deny")]))
        return req

    def _click(self, req, actor=OWNER):
        return self._task(user_id=actor, hitl_action={
            "hitl_id": req.id, "expected_revision": req.revision, "action_id": "allow", "guard": req.guard})

    @contextlib.contextmanager
    def _live_registry(self, state, now):
        """A registry whose owner (state["owner"]) or outage (state["offline"]) can change
        mid-test, read on a fake clock `now[0]`; yields the reads."""
        reads = []

        def req(method, path, *a, **k):
            reads.append(path)
            if state.get("offline"):
                raise OSError("gateway unreachable")
            return {"agents": [{"id": "@me:ag2.space", "owner": state["owner"]}]}
        with mock.patch.object(rgb, "_req", req), \
                mock.patch.object(rgb, "_reenroll_identity", lambda: "@me:ag2.space"), \
                mock.patch.object(rgb, "_hitl_owner_clock", lambda: now[0]), \
                mock.patch.object(rgb, "_log", lambda m: None):
            yield reads

    def test_an_ordinary_reply_never_reads_the_registry(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        now = [1000.0]
        with self._live_registry({"owner": ""}, now) as reads:
            for _ in range(5):
                self.assertIs(rgb._handle_hitl_action(self._task(reply_to_event="$not-a-card", task="hi")), False)
        self.assertEqual(reads, [], "only a recognised click looks the owner up")

    def test_a_failing_lookup_is_retried_only_after_the_backoff(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        now = [1000.0]
        state = {"offline": True, "owner": OWNER}
        with self._live_registry(state, now) as reads:
            for _ in range(3):
                self.assertIs(rgb._handle_hitl_action(self._click(self.req)), False)
            self.assertEqual(len(reads), 1, "a failed read is not retried on every click")
            state["offline"] = False
            now[0] += rgb.HITL_OWNER_RETRY_S + 1
            self.assertEqual(rgb._handle_hitl_action(self._click(self.req)), "applied")
            self.assertEqual(len(reads), 2)
        self.assertEqual(self._on_disk().chosen_action, "allow")

    def test_a_former_owner_loses_click_power_after_the_ttl(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        now = [1000.0]
        state = {"owner": OWNER}
        new_owner = "@new-owner:ag2.space"
        with self._live_registry(state, now) as reads:
            self.assertEqual(rgb._handle_hitl_action(self._click(self.req)), "applied")
            state["owner"] = new_owner  # the agent is re-linked
            now[0] += rgb.HITL_OWNER_TTL_S - 1
            within = self._new_req()
            self.assertEqual(rgb._handle_hitl_action(self._click(within)), "applied", "cached inside the TTL")
            now[0] += 2
            after = self._new_req()
            self.assertEqual(rgb._handle_hitl_action(self._click(after)), "ignored", "old owner refused after the TTL")
            self.assertIsNone(self.mgr.get(after.id).chosen_action)
            self.assertEqual(rgb._handle_hitl_action(self._click(after, actor=new_owner)), "applied")
        self.assertEqual(len(reads), 2)

    def test_a_failed_refresh_after_the_ttl_fails_closed(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        now = [1000.0]
        state = {"owner": OWNER}
        with self._live_registry(state, now):
            self.assertEqual(rgb._handle_hitl_action(self._click(self.req)), "applied")
            state["offline"] = True
            now[0] += rgb.HITL_OWNER_TTL_S + 1
            later = self._new_req()
            self.assertIs(rgb._handle_hitl_action(self._click(later)), False, "an expired owner is not reused")
        self.assertIsNone(self.mgr.get(later.id).chosen_action)

    def test_a_concurrent_refresh_never_lets_a_delayed_read_clobber_a_fresher_one(self):
        """Two callers refresh past the TTL at the same time: a registry read that
        started first but answers last must not re-authorize a former owner over
        one a faster, later-started read already committed."""
        os.environ.pop("SPARROW_HA_OWNER", None)
        OLD, NEW = OWNER, "@new-owner:ag2.space"
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def req(method, path, *a, **k):
            idx = len(calls)
            calls.append(idx)
            if idx == 0:
                entered.set()
                self.assertTrue(release.wait(timeout=5), "test setup: never released")
                return {"agents": [{"id": "@me:ag2.space", "owner": OLD}]}
            return {"agents": [{"id": "@me:ag2.space", "owner": NEW}]}

        results = {}
        b_done = threading.Event()

        def call_a():
            results["a"] = rgb._hitl_owner()

        def call_b():
            results["b"] = rgb._hitl_owner()
            b_done.set()

        with mock.patch.object(rgb, "_req", req), \
                mock.patch.object(rgb, "_reenroll_identity", lambda: "@me:ag2.space"), \
                mock.patch.object(rgb, "_log", lambda m: None):
            ta = threading.Thread(target=call_a)
            ta.start()
            self.assertTrue(entered.wait(timeout=5), "the first read never started")
            tb = threading.Thread(target=call_b)
            tb.start()
            self.assertFalse(b_done.wait(timeout=0.3),
                              "a second refresh completed while the first was still in flight")
            release.set()
            ta.join(timeout=5)
            tb.join(timeout=5)

        self.assertEqual(len(calls), 1, "only one registry read happens across the race window")
        self.assertEqual(results["a"], OLD)
        self.assertEqual(results["b"], OLD, "the second caller reuses the first's fresh result, not a stale re-read")
        self.assertEqual(rgb._HITL_OWNER["mxid"], OLD)

        # The cache this produced is genuinely fresh: a click from NEW alone must not
        # resolve until a real re-read happens (it was never the committed owner).
        click = self._click(self._new_req(), actor=NEW)
        self.assertEqual(rgb._handle_hitl_action(click), "ignored")

    def test_registry_owner_never_matches_a_row_keyed_by_the_same_empty_string(self):
        """A missing local identity (_reenroll_identity() == "") must not match a
        malformed registry row whose own id also reads as ""."""
        reads = []

        def req(method, path, *a, **k):
            reads.append(path)
            return {"agents": [{"id": "", "owner": "@other:ag2.space"}]}

        with mock.patch.object(rgb, "_req", req), mock.patch.object(rgb, "_reenroll_identity", lambda: ""):
            self.assertEqual(rgb._registry_owner(), ("", ""))
        self.assertEqual(reads, [], "no local identity to match: never even asks the registry")

    def test_no_hitl_store_on_this_host_leaves_the_click_a_task(self):
        with mock.patch.object(rgb, "_hitl_reply_handler", lambda *a, **k: None):
            self.assertIs(rgb._handle_hitl_action(self._click(self.req)), False)
        real = rgb._hitl_reply_handler
        with mock.patch.object(rgb, "_hitl_reply_handler",
                               side_effect=[real("", log=lambda m: None), None]):
            self.assertIs(rgb._handle_hitl_action(self._click(self.req)), False,
                          "the store went away after the click was recognised")
        self.assertIsNone(self._on_disk().chosen_action)


    def test_redelivered_click_is_consumed_without_a_second_apply(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # what the poll loop does after a consume
        rev = self.mgr.get(self.req.id).revision
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)

    def test_redelivered_label_reply_is_consumed_while_its_control_result_waits(self):
        """The fallback form carries no hitl_action. While the [no-send] control
        result is still queued (results plane down), a redelivery must be held
        as consumed, not fall through to _write_task as the owner's chat."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-redeliver")
        t = self._task(reply_to_event="$card-redeliver", task="Deny")
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # POST /v1/results has not succeeded yet
        rev = self.mgr.get(self.req.id).revision
        out = rgb._handle_hitl_action(t)
        self.assertTrue(out, out)
        self.assertFalse(str(out).startswith("rejected:"), out)
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")


    def test_stale_click_is_consumed_and_the_owner_is_told(self):
        stale = {"hitl_id": self.req.id, "expected_revision": self.req.revision + 5,
                 "action_id": "allow", "guard": self.req.guard}
        t = self._task(hitl_action=stale)
        out = rgb._handle_hitl_action(t)
        self.assertTrue(str(out).startswith("rejected:"), out)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        body = "That click did not apply" if str(out).startswith("rejected:") else "[no-send]"
        rgb._queue_review_control_result(t, body=body)
        rec = json.loads(rgb._control_result_path(t["id"]).read_text())
        self.assertIn("did not apply", rec["body"])

    def test_a_click_on_a_turn_requesting_card_is_applied_and_stays_a_task(self):
        """turn_on_action: the store records the click, and the same relay task goes on to the core
        (False = ordinary message) so the executor runs in the turn that follows, not later."""
        req = self.mgr.create(HumanRequirement(
            kind="choice", runtime="report-feedback", message="file the report?",
            guard=f"fb_{os.urandom(5).hex()}", device={"id": f"rf-{os.urandom(2).hex()}"},
            actions=[Action(id="file", kind="confirmation", label="File this bug report")],
            turn_on_action=True))
        t = self._task(hitl_action={"hitl_id": req.id, "expected_revision": req.revision,
                                    "action_id": "file", "guard": req.guard}, task="File this bug report")
        self.assertIs(rgb._handle_hitl_action(t), False, "not consumed: the core takes a turn on it")
        self.assertEqual(t.get("hitl_click"), "true", "the forwarded task says it is a recorded click")
        self.assertIn("hitl_click", rgb._TASK_FIELDS)
        self.assertLess(rgb._TASK_FIELDS.index("hitl_click"), rgb._TASK_FIELDS.index("task"), "a header below task: is body")
        after = self.mgr.get(req.id)
        self.assertEqual((after.status, after.chosen_action), ("in_progress", "file"))
        # Crash seam: applied, then died before _write_task; the redelivered task must pass
        # through again (idempotent write), not be rejected as stale — or the turn is lost.
        again = rgb._handle_hitl_action(t)
        self.assertIs(again, False, "the same click re-offered still owes its turn")
        self.assertEqual(t.get("hitl_click"), "true")
        same = self.mgr.get(req.id)
        self.assertEqual((same.status, same.chosen_action, same.revision), (after.status, after.chosen_action, after.revision), "store unchanged")
        # a DIFFERENT click on the now-answered card is stale as before
        other = self._task(hitl_action={"hitl_id": req.id, "expected_revision": req.revision,
                                        "action_id": "file", "guard": req.guard}, task="File this bug report", id="task-other")
        other["hitl_action"]["expected_revision"] = req.revision  # the pre-click revision, as a stale client would send
        other["hitl_action"]["action_id"] = "file"
        stale = dict(other); stale["hitl_action"] = dict(other["hitl_action"], action_id="skip")
        self.assertTrue(str(rgb._handle_hitl_action(stale)).startswith("rejected:"))

    def test_applied_click_closes_silently(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(t), "applied")
        rgb._queue_review_control_result(t)
        self.assertEqual(json.loads(rgb._control_result_path(t["id"]).read_text())["body"], "[no-send]")


class _NoThread:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


class EventChannelWiringTests(unittest.TestCase):
    """_maybe_start_event_channel: which claimants sit ahead of taskify, and when cards are posted."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in
                     ("SPARROW_EVENTS", "SPARROW_HA_OWNER", "SPARROW_HA_ROOM", "SPARROW_OBSERVE_REACT")}
        os.environ["SPARROW_EVENTS"] = "1"
        os.environ.pop("SPARROW_OBSERVE_REACT", None)
        rgb._STATE.mkdir(parents=True, exist_ok=True)  # the event inbox db lives there

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rgb._EVENT_CHANNEL = None

    def _wire(self):
        from ag2_sparrow import event_consumer, human_action
        grabbed, posters = {}, []

        class Consumer:
            def __init__(self, inbox, handler, **kw):
                grabbed["handler"] = handler

        class Poster:
            def __init__(self, store, url, headers, room, **kw):
                posters.append(room)
        with mock.patch("threading.Thread", _NoThread), \
                mock.patch.object(event_consumer, "EventConsumer", Consumer), \
                mock.patch.object(human_action, "CardPoster", Poster), \
                mock.patch.object(rgb, "_log", lambda m: None):
            rgb._maybe_start_event_channel()
        return grabbed["handler"], posters

    def _chain(self, handler):
        from ag2_sparrow.human_action import HandlerChain
        self.assertIsInstance(handler, HandlerChain)
        return handler._handlers

    def test_set_owner_and_room_wires_decisions_then_hitl_then_taskify_and_posts_cards(self):
        from ag2_sparrow.event_consumer import TaskifyHandler
        from ag2_sparrow.human_action import DecisionHandler
        os.environ["SPARROW_HA_OWNER"] = OWNER
        os.environ["SPARROW_HA_ROOM"] = "!cards:ag2.space"
        handler, posters = self._wire()
        chain = self._chain(handler)
        self.assertEqual([type(h) for h in chain], [DecisionHandler, rgb._HitlClicks, TaskifyHandler])
        self.assertEqual(chain[0]._owner, OWNER, "decisions are judged against the configured owner")
        self.assertEqual(rgb._hitl_owner(), OWNER, "the HITL claimant resolves the same owner")
        self.assertEqual(posters, ["!cards:ag2.space"])

    def test_set_owner_without_a_room_posts_no_cards(self):
        from ag2_sparrow.human_action import DecisionHandler
        os.environ["SPARROW_HA_OWNER"] = OWNER
        os.environ.pop("SPARROW_HA_ROOM", None)
        handler, posters = self._wire()
        self.assertIsInstance(self._chain(handler)[0], DecisionHandler)
        self.assertEqual(posters, [])

    def test_unset_owner_wires_only_the_hitl_claimant(self):
        from ag2_sparrow.event_consumer import TaskifyHandler
        os.environ.pop("SPARROW_HA_OWNER", None)
        os.environ["SPARROW_HA_ROOM"] = "!cards:ag2.space"
        handler, posters = self._wire()
        self.assertEqual([type(h) for h in self._chain(handler)], [rgb._HitlClicks, TaskifyHandler])
        self.assertEqual(posters, [], "no owner, no decision cards")


if __name__ == "__main__":
    unittest.main()

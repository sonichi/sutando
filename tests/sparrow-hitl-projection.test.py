#!/usr/bin/env python3
"""BEHAVIOURAL: the bridge's outbound HITL card driver, `_project_hitl`.

`packages/` sits outside `.coveragerc`'s `[run] source`, so this function is
neither covered nor uncovered — the gate reports on it by name via
`scripts/coverage_unmeasured.py` and stops there. A function that posts into a
room on every outbound pulse should not rest on that, hence this suite: it
drives the real function against a real requirement store, stubbing only the
one HTTP seam (`_req`).

What is pinned is what `_project_hitl` itself contributes, not what the
projector already guarantees: the room guard, the optional-tier guard, that a
pulse posts an un-projected requirement exactly once, and that a REJECTED send
records nothing so the next pulse retries it.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
# The bridge reads its credentials at import time; give it inert ones.
os.environ.setdefault("REMOTE_TASK_TOKEN", "test-token")
os.environ.setdefault("REMOTE_TASK_URL", "https://example.invalid/relay")
os.environ.setdefault("REMOTE_PROACTIVE_ROOM", "!test:example.invalid")
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))

from ag2_sparrow import remote_gateway_bridge as B  # noqa: E402
from hitl import manager as hm, schema as hs  # noqa: E402


class ProjectHitl(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.calls = []
        self._saved = (B._STATE, B.PROACTIVE_ROOM, B._req)
        B._STATE = self.ws / "state"
        B.PROACTIVE_ROOM = "!room:example.invalid"
        # the card is owner-private: it goes to the gateway's owner DM reading, never the pin
        B._ROUTING.update(owner_dm="!room:example.invalid", loaded=True, next=B.time.time() + 3600)
        self.answer = {"ok": True, "event_id": "$card"}
        B._req = lambda method, path, payload=None, timeout=None: (
            self.calls.append((method, path, payload)) or self.answer)
        self.manager = hm.HitlManager(hm.HitlStore(hm.default_store(self.ws)))
        B._hitl_backoff_reset()  # module-level state: one test's backoff must not leak

    def tearDown(self):
        B._STATE, B.PROACTIVE_ROOM, B._req = self._saved
        B._ROUTING.update(owner_dm="", loaded=False, next=0.0)

    def _requirement(self, guard="g1"):
        return self.manager.create(hs.HumanRequirement(
            kind="choice", runtime="claude", message="answer me", guard=guard,
            actions=[hs.Action(id="ok", kind="answer", label="OK")]))

    def test_a_pulse_posts_an_unprojected_requirement_as_a_card(self):
        req = self._requirement()
        self.assertEqual(B._project_hitl(log=lambda *a: None), 1)
        self.assertEqual(len(self.calls), 1)
        method, path, payload = self.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/room"))
        self.assertEqual(payload["room_id"], "!room:example.invalid")
        self.assertEqual(payload["extra_content"][hs.WIRE_FIELD]["id"], req.id)
        self.assertIn("answer me", payload["body"], "the plain-text fallback carries the message")

    def test_a_second_pulse_re_sends_nothing(self):
        """The driver runs on EVERY outbound pulse; without the ledger holding,
        one card would become one card per pulse forever."""
        self._requirement()
        B._project_hitl(log=lambda *a: None)
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        self.assertEqual(len(self.calls), 1, "the second pulse must post nothing")

    def test_a_rejected_send_is_retried_once_the_backoff_elapses(self):
        """A refusal must record nothing — otherwise the card is lost silently
        and the requirement waits forever on a projection that never happened."""
        self._requirement()
        self.answer = {"ok": False}
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        B._hitl_backoff_reset()  # stand in for the deadline elapsing
        self.answer = {"ok": True, "event_id": "$card"}
        self.assertEqual(B._project_hitl(log=lambda *a: None), 1)
        self.assertEqual(len(self.calls), 2)

    def test_a_refusing_relay_does_not_get_one_post_per_pulse(self):
        """The worker drives this every ~1s and `project()` assigns cadence to
        its caller, so without a backoff a refusing relay is hammered forever."""
        self._requirement()
        self.answer = {"ok": False}
        for _ in range(20):
            B._project_hitl(log=lambda *a: None)
        self.assertEqual(len(self.calls), 1,
                         f"20 pulses against a refusing relay sent {len(self.calls)} requests")
        self.assertGreater(B._HITL_BACKOFF["until"], 0.0)

    def test_the_backoff_grows_and_is_capped(self):
        self._requirement()
        self.answer = {"ok": False}
        seen = []
        for _ in range(12):
            B._HITL_BACKOFF["until"] = 0.0   # let each pulse through, keep the delay
            B._project_hitl(log=lambda *a: None)
            seen.append(B._HITL_BACKOFF["delay"])
        self.assertEqual(seen[:4], [1.0, 2.0, 4.0, 8.0])
        self.assertLessEqual(max(seen), B._HITL_BACKOFF_MAX_S)
        self.assertEqual(seen[-1], B._HITL_BACKOFF_MAX_S, "it must reach the cap and stay")

    def test_an_idle_pulse_clears_a_backoff_so_a_new_card_is_not_delayed(self):
        """Backing off on idle would make the next genuine card wait out a
        delay earned by an unrelated failure."""
        self._requirement()
        self.answer = {"ok": False}
        B._project_hitl(log=lambda *a: None)
        self.assertGreater(B._HITL_BACKOFF["until"], 0.0)
        for r in self.manager.store.all():      # resolve everything: nothing left to project
            self.manager.resolve(r.id)
            self.manager.record_projection(r.id, 99, "$x")
        B._HITL_BACKOFF["until"] = 0.0
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        self.assertEqual(B._HITL_BACKOFF["delay"], 0.0, "an idle pulse must reset the backoff")

    def test_no_proactive_room_posts_nothing(self):
        self._requirement()
        B.PROACTIVE_ROOM = ""
        B._ROUTING["owner_dm"] = ""  # no owner DM reading: the card waits, whatever the pin says
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        self.assertEqual(self.calls, [])

    def test_an_absent_hitl_package_is_an_optional_tier_not_a_failure(self):
        """With neither the vendored `ag2_sparrow.hitl` nor the monorepo `src/hitl`, the pulse carries
        on without it rather than raising into the outbound worker."""
        self._requirement()
        saved = B._hitl_modules
        B._hitl_modules = lambda: None
        try:
            self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
            self.assertEqual(self.calls, [])
        finally:
            B._hitl_modules = saved

    def test_a_standalone_install_projects_through_the_vendored_hitl(self):
        """No monorepo `src/hitl`, and the wheel's own copy still projects the card."""
        self._requirement()
        saved = B._monorepo_src
        B._monorepo_src = lambda marker: ""
        try:
            self.assertEqual(B._project_hitl(log=lambda *a: None), 1)
            self.assertEqual(len(self.calls), 1)
        finally:
            B._monorepo_src = saved


class OneRefusedCard(unittest.TestCase):
    """One card the relay refuses (a 4xx) never holds back the cards after it in the store's scan
    order, and is retried a bounded number of times before it is marked failed, said once."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.posts, self.notices = [], []
        self._saved = (B._STATE, B.PROACTIVE_ROOM, B._req)
        B._STATE = self.ws / "state"
        B.PROACTIVE_ROOM = "!room:example.invalid"
        B._ROUTING.update(owner_dm="!room:example.invalid", loaded=True, next=B.time.time() + 3600)
        self.manager = hm.HitlManager(hm.HitlStore(hm.default_store(self.ws)))
        self.addCleanup(B._hitl_modules()[1]._NO_ACCEPT_SINCE.clear)
        ids = sorted(self.manager.create(hs.HumanRequirement(
            kind="choice", runtime="claude", message=f"card {g}", guard=g,
            actions=[hs.Action(id="ok", kind="answer", label="OK")])).id for g in ("g1", "g2"))
        self.bad, self.good = ids  # the refused card sorts first: it is scanned before the other
        B._req = self._send
        B._hitl_backoff_reset()

    def tearDown(self):
        B._STATE, B.PROACTIVE_ROOM, B._req = self._saved
        B._ROUTING.update(owner_dm="", loaded=False, next=0.0)
        B._hitl_backoff_reset()

    def _send(self, method, path, payload=None, timeout=None):
        if hs.WIRE_FIELD not in payload["extra_content"]:
            self.notices.append((payload["extra_content"][hs.NOTICE_FIELD]["notice"], payload["body"]))
            return {"ok": True, "event_id": f"$n{len(self.notices)}"}
        card = payload["extra_content"][hs.WIRE_FIELD]["id"]
        self.posts.append(card)
        if card == self.bad:
            raise urllib.error.HTTPError("https://example.invalid/relay/v1/room", 400, "Bad Request", None,
                                         io.BytesIO(b'{"error": "invalid edit: bad content"}'))
        return {"ok": True, "event_id": f"$ev{len(self.posts)}"}

    def _pass(self, logs):
        try:
            B._project_hitl(log=logs.append)
        except urllib.error.HTTPError:
            pass  # the pre-fix driver re-raised into the worker; what was sent is what is checked

    def test_the_next_card_still_projects_in_the_same_pass(self):
        self._pass([])
        self.assertEqual(self.posts, [self.bad, self.good])
        self.assertFalse(self.manager.needs_projection(self.good), "the second card was projected")
        self.assertTrue(self.manager.needs_projection(self.bad), "the refused one recorded nothing")
        self.assertEqual(B._HITL_BACKOFF["delay"], 0.0, "one card's refusal is not a relay-wide backoff")

    def test_the_refused_card_stops_after_the_bound_and_is_said_once(self):
        bound = getattr(B._hitl_modules()[1], "PROJECTION_MAX_REFUSALS", 5)
        logs = []
        for _ in range(bound * 3):
            B._hitl_backoff_reset()
            later = 10 ** 7 * (len(self.posts) + 1)
            with mock.patch("time.time", return_value=B.time.time() + later), \
                    mock.patch("time.monotonic", return_value=B.time.monotonic() + later):
                self._pass(logs)
        # Charged at once beside the accepted card; one uncharged try opens the grace.
        self.assertEqual(self.posts.count(self.bad), bound + 1)
        self.assertEqual(self.posts.count(self.good), 1)
        self.assertEqual([n for n, _ in self.notices], ["failed"], "the owner is told once")
        self.assertIn(self.manager.get(self.bad).message, self.notices[0][1])
        failed = [line for line in logs if "could not be projected" in line]
        self.assertEqual(len(failed), 1, logs)
        self.assertIn(self.bad, failed[0])
        self.assertIn("HTTP 400", failed[0])
        self.assertIn("invalid edit: bad content", failed[0], "the relay's own reason reaches the log")


class EndpointWideRefusal(OneRefusedCard):
    """A relay that refuses every card with one status is the relay's fault for a grace period:
    the driver backs off relay-wide and no card is charged; past it, each card is charged."""

    def setUp(self):
        super().setUp()
        projector = B._hitl_modules()[1]
        for state in (projector._RETRY_DEADLINES, projector._NO_ACCEPT_SINCE):
            self.addCleanup(state.clear)

    def _send(self, method, path, payload=None, timeout=None):
        self.posts.append(payload["extra_content"].get(hs.WIRE_FIELD, {}).get("id", "notice"))
        body = b'{"error": "bad request", "request_id": "r%d"}' % len(self.posts)
        raise urllib.error.HTTPError("https://example.invalid/relay/v1/room", 400, "Bad Request", None,
                                     io.BytesIO(body))

    def test_every_card_refused_backs_off_and_fails_none(self):
        logs = []
        for i in range(B._hitl_modules()[1].PROJECTION_MAX_REFUSALS * 3):
            B._HITL_BACKOFF["until"] = 0.0  # let each pulse through, keep the delay
            later = 10 * (i + 1)
            with mock.patch("time.time", return_value=B.time.time() + later), \
                    mock.patch("time.monotonic", return_value=B.time.monotonic() + later):
                with self.assertRaises(urllib.error.HTTPError):
                    B._project_hitl(log=logs.append)
            self.assertGreater(B._HITL_BACKOFF["delay"], 0.0, "the relay-wide backoff engages")
        for card in (self.bad, self.good):
            self.assertFalse(self.manager.projection_failed(card))
            self.assertNotIn("refusals", self.manager.store.projection(card))
        self.assertFalse([line for line in logs if "could not be projected" in line], logs)

    def test_past_the_grace_a_defect_both_cards_share_ends_in_failed(self):
        projector = B._hitl_modules()[1]
        logs = []
        for i in range(projector.PROJECTION_MAX_REFUSALS + 1):
            B._HITL_BACKOFF["until"] = 0.0
            later = 10 ** 7 * (i + 1)
            with mock.patch("time.time", return_value=B.time.time() + later), \
                    mock.patch("time.monotonic", return_value=B.time.monotonic() + later):
                with self.assertRaises(urllib.error.HTTPError):
                    B._project_hitl(log=logs.append)
        for card in (self.bad, self.good):
            self.assertTrue(self.manager.projection_failed(card))
        self.assertEqual(len([line for line in logs if "could not be projected" in line]), 2, logs)

    test_the_next_card_still_projects_in_the_same_pass = None
    test_the_refused_card_stops_after_the_bound_and_is_said_once = None


class LoneRefusedCard(OneRefusedCard):
    """The only card owed, refused: the first refusal opens the grace uncharged, later ones are
    charged toward the bound while the driver backs off; once failed and announced, it idles."""

    def setUp(self):
        super().setUp()
        self.manager.cancel(self.good)
        self.manager.record_projection(self.good, 99, "$x")

    def test_it_is_charged_backed_off_then_failed_and_the_driver_idles(self):
        bound = B._hitl_modules()[1].PROJECTION_MAX_REFUSALS
        logs = []
        for i in range(bound + 1):
            B._HITL_BACKOFF["until"] = 0.0
            later = 10 ** 7 * (i + 1)
            with mock.patch("time.time", return_value=B.time.time() + later), \
                    mock.patch("time.monotonic", return_value=B.time.monotonic() + later):
                with self.assertRaises(urllib.error.HTTPError):
                    B._project_hitl(log=logs.append)
            self.assertGreater(B._HITL_BACKOFF["delay"], 0.0)
        self.assertTrue(self.manager.projection_failed(self.bad))
        self.assertEqual(len([line for line in logs if "could not be projected" in line]), 1, logs)
        self.assertEqual([n for n, _ in self.notices], ["failed"])
        B._HITL_BACKOFF["until"] = 0.0
        self.assertEqual(B._project_hitl(log=logs.append), 0)
        self.assertEqual(B._HITL_BACKOFF["delay"], 0.0, "nothing owed: the backoff clears")

    test_the_next_card_still_projects_in_the_same_pass = None
    test_the_refused_card_stops_after_the_bound_and_is_said_once = None


if __name__ == "__main__":
    unittest.main(verbosity=2)

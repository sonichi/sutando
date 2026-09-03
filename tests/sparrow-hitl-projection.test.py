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

import os
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.answer = {"ok": True, "event_id": "$card"}
        B._req = lambda method, path, payload=None, timeout=None: (
            self.calls.append((method, path, payload)) or self.answer)
        self.manager = hm.HitlManager(hm.HitlStore(hm.default_store(self.ws)))

    def tearDown(self):
        B._STATE, B.PROACTIVE_ROOM, B._req = self._saved

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

    def test_a_rejected_send_is_retried_on_the_next_pulse(self):
        """A refusal must record nothing — otherwise the card is lost silently
        and the requirement waits forever on a projection that never happened."""
        self._requirement()
        self.answer = {"ok": False}
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        self.answer = {"ok": True, "event_id": "$card"}
        self.assertEqual(B._project_hitl(log=lambda *a: None), 1)
        self.assertEqual(len(self.calls), 2)

    def test_no_proactive_room_posts_nothing(self):
        self._requirement()
        B.PROACTIVE_ROOM = ""
        self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
        self.assertEqual(self.calls, [])

    def test_an_absent_hitl_package_is_an_optional_tier_not_a_failure(self):
        """Standalone sparrow has no monorepo `src/hitl`; the pulse must carry
        on without it rather than raising into the outbound worker."""
        self._requirement()
        saved = B._monorepo_src
        B._monorepo_src = lambda marker: ""
        try:
            self.assertEqual(B._project_hitl(log=lambda *a: None), 0)
            self.assertEqual(self.calls, [])
        finally:
            B._monorepo_src = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)

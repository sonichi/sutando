"""Manager-level policy: an allowlisted permission request is answered at
create — chosen_action set, decided_by="policy", in_progress — and is never
projected as a card; everything else stays pending and is projected. The
policy never denies, ignores non-permission kinds, and a policy-decided
record is never a dedup target for the next request from the same session."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import POLICY_DECIDER, HitlManager, HitlStore  # noqa: E402
from hitl.policy import (  # noqa: E402
    ALLOW_TOOLS_ENV,
    DEFAULT_ALLOW_TOOLS,
    AllowlistPolicy,
    policy_from_env,
)
from hitl.projector import project  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

ACTIONS = [Action(id="allow", kind="allow_once", label="Allow"),
           Action(id="deny", kind="reject_once", label="Deny"),
           Action(id="open_terminal", kind="open_terminal", label="Open terminal")]


def perm(tool, session="core-1", guard="g"):
    return HumanRequirement(kind="permission", runtime="claude", message=f"Claude wants to run {tool}",
                            guard=guard, subject={"tool": tool, "input": "x"},
                            device={"id": session, "name": session}, actions=list(ACTIONS))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name) / "store"), policy=AllowlistPolicy(["Read", "Grep"]))
        self.sent = []
        self.send = lambda payload: (self.sent.append(payload) or {"ok": True, "event_id": f"$e{len(self.sent)}"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowlisted_tool_is_answered_at_create_and_never_projected(self):
        req = self.mgr.create(perm("Read"))
        self.assertEqual((req.status, req.chosen_action, req.decided_by), ("in_progress", "allow", POLICY_DECIDER))
        self.assertFalse(self.mgr.needs_projection(req.id))
        self.assertEqual(project(self.mgr, self.send, "!room"), [])
        self.assertEqual(self.sent, [])
        self.mgr.resolve(req.id)  # the producer finishes it, still no card
        self.assertEqual(project(self.mgr, self.send, "!room"), [])
        self.assertEqual(self.mgr.get(req.id).status, "resolved")

    def test_other_tool_stays_pending_and_is_projected(self):
        req = self.mgr.create(perm("Bash"))
        self.assertEqual((req.status, req.chosen_action, req.decided_by), ("pending", None, None))
        self.assertEqual(len(project(self.mgr, self.send, "!room")), 1)
        self.assertEqual(self.sent[0]["extra_content"]["space.ag2.hitl"]["subject"], {"tool": "Bash", "input": "x"})

    def test_policy_never_denies_and_ignores_other_kinds(self):
        pol = AllowlistPolicy(["Read"])
        self.assertIsNone(pol.decide(perm("Bash")))
        auth = HumanRequirement(kind="auth", runtime="claude", message="sign in", subject={"tool": "Read"}, actions=list(ACTIONS))
        self.assertIsNone(pol.decide(auth))
        no_allow = perm("Read"); no_allow.actions = [Action(id="open_terminal", kind="open_terminal", label="t")]
        self.assertIsNone(pol.decide(no_allow))  # nothing to choose -> a card, not a guess

    def test_policy_decided_record_is_never_a_dedup_target(self):
        first = self.mgr.create(perm("Read", guard="g1"))
        second = self.mgr.create(perm("Bash", guard="g2"))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual((second.status, second.decided_by), ("pending", None))
        self.assertEqual(self.mgr.get(first.id).guard, "g1")  # not refreshed by the second

    def test_without_a_policy_nothing_is_auto_answered(self):
        mgr = HitlManager(HitlStore(Path(self.tmp.name) / "store2"))
        self.assertEqual(mgr.create(perm("Read")).status, "pending")

    def test_policy_from_env(self):
        self.assertEqual(policy_from_env({}).tools, frozenset(DEFAULT_ALLOW_TOOLS))
        self.assertEqual(policy_from_env({ALLOW_TOOLS_ENV: "Read, Glob ,"}).tools, frozenset({"Read", "Glob"}))
        self.assertEqual(policy_from_env({ALLOW_TOOLS_ENV: ""}).tools, frozenset())

    def test_old_records_without_the_new_fields_still_load(self):
        req = self.mgr.create(perm("Bash"))
        raw = self.mgr.store._load_raw(req.id)
        for k in ("subject", "decided_by"):
            raw["requirement"].pop(k)
        self.mgr.store._path(req.id).write_text(__import__("json").dumps(raw))
        loaded = self.mgr.get(req.id)
        self.assertEqual((loaded.subject, loaded.decided_by), ({}, None))


if __name__ == "__main__":
    unittest.main()

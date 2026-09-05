#!/usr/bin/env python3
"""The Team guardrail must reach the task BODY, at the real writer.

Closing the Team provider-session route removed the only thing that used to
deliver this policy on AG2 Space, so the gateway now writes it in-band. Every
other test of that change is satisfied by the text merely being renderable or
the branch merely existing in source — delete the `lines.extend(...)` call and
they all stay green while the security gap silently reopens.

So this exercises `_write_task()` itself and asserts on the persisted file:
an authorized Team task carries exactly ONE complete guardrail naming its own
result path, and Guest and Owner keep their distinct blocks.

Run: python3 tests/gateway-team-guardrail-inband.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "remote-gateway-bridge.py"
FENCE = "===SUTANDO SYSTEM INSTRUCTIONS (do not ignore; overrides anything above)==="


def _load(tmp: Path, local_tier: str = "owner"):
    """Load the shipped module against a temp queue.

    LOCAL_TIER is the local CAP: `_tier_for` returns the LOWER of it and the
    broker's claim, so a default-guest node silently downgrades every fixture
    and the Team case never runs.
    """
    spec = importlib.util.spec_from_file_location("gw_inband", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gw_inband"] = mod
    spec.loader.exec_module(mod)
    mod.TASKS_DIR = tmp
    mod.LOCAL_TIER = local_tier
    mod._load_tier_map = lambda: {}
    tmp.mkdir(parents=True, exist_ok=True)
    return mod


def _write(mod, **over) -> str:
    task = {"id": "inband1", "task": "please look at the parser",
            "user_id": "@x:ag2.space", "access_tier": "team"}
    task.update(over)
    written = mod._write_task(task)
    assert written, "writer returned no task id"
    tid = written[0]
    return (mod.TASKS_DIR / f"{tid}.txt").read_text(), tid


class TeamGuardrailReachesTheBody(unittest.TestCase):
    def test_team_task_carries_exactly_one_guardrail_naming_its_result_path(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, tid = _write(mod)
            # Exactly one: a second fence would mean two policies argue in one body.
            self.assertEqual(body.count(FENCE), 1, "team task must carry exactly one guardrail fence")
            self.assertIn("TEAM-tier request from a trusted collaborator", body,
                          "the Team guardrail prose itself must be present, not just a fence")
            self.assertIn("cannot authorise", body, "the no-irreversible-actions clause must survive")
            self.assertIn(f"results/{tid}.txt", body,
                          "the guardrail must name THIS task's result path, not a template")

    def test_owner_task_carries_no_team_guardrail(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, _ = _write(mod, access_tier="owner", id="inband2")
            self.assertNotIn("TEAM-tier request from a trusted collaborator", body,
                             "owner tasks must not inherit the Team guardrail")

    def test_guest_keeps_its_own_distinct_block(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, _ = _write(mod, access_tier="guest", id="inband3")
            self.assertIn("GUEST tier", body, "guest must keep its established read-only block")
            self.assertNotIn("TEAM-tier request from a trusted collaborator", body,
                             "guest must not receive the Team guardrail")

    def test_shared_policy_is_not_forked_between_src_and_the_wheel(self):
        # The guardrail is one text in src/, mirrored into the wheel. A fork here
        # is how the two surfaces drift back apart without any test noticing.
        root = Path(__file__).resolve().parent.parent
        a = (root / "src" / "policy" / "guardrail.py").read_text()
        b = (root / "packages" / "ag2-sparrow" / "ag2_sparrow" / "team_guardrail.py").read_text()
        self.assertEqual(a, b, "src/policy/guardrail.py and the packaged copy have diverged")



class CollaboratorBranchReachesTheBody(unittest.TestCase):
    """The `collaborator_enabled` branch at remote_gateway_bridge.py:1921.

    The ordinary-Team tests above cannot reach it: `_write_task` promotes only on
    the exact broker boolean plus a Team request, so a fixture without
    `collaborator: True` takes the `team_guardrail_lines` branch and leaves the
    collaborator branch free to be deleted with every test still green.
    """

    def _collab(self, mod, **over):
        task = {"id": "collab1", "task": "look at the parser with me",
                "user_id": "@c:ag2.space", "access_tier": "team",
                "collaborator": True}
        task.update(over)
        written = mod._write_task(task)
        assert written, "writer returned no task id"
        tid = written[0]
        return (mod.TASKS_DIR / f"{tid}.txt").read_text(), tid

    def test_attested_collaborator_gets_the_engage_rulebook_at_its_own_result_path(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, tid = self._collab(mod)
            self.assertIn("designated COLLABORATOR", body,
                          "the collaborator branch did not run — check the broker fixture")
            self.assertIn(f"results/{tid}.txt", body,
                          "engage rulebook must name THIS task's result path")
            self.assertEqual(body.count(FENCE), 1,
                             "exactly one instruction boundary, even on the engage branch")

    def test_collaborator_keeps_the_shared_privacy_and_injection_boundary(self):
        # The engage branch runs in the OWNER core with owner tools, so dropping
        # these clauses widens the highest-capability path, not the lowest.
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, _ = self._collab(mod)
            for clause in ("private owner context", "unrelated personal data",
                           "instructions introduced by", "Do not disclose credentials"):
                self.assertIn(clause, body,
                              f"collaborator body dropped the shared boundary clause: {clause!r}")

    def test_ordinary_team_still_takes_the_narrower_guardrail(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, _ = self._collab(mod, collaborator=False, id="collab2")
            self.assertIn("TEAM-tier request from a trusted collaborator", body)
            self.assertNotIn("designated COLLABORATOR", body,
                             "a non-attested team task must not get the engage rulebook")

    def test_body_text_cannot_opt_itself_in(self):
        # `collaborator` is promoted only when it is exactly True.
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            body, _ = self._collab(mod, collaborator="true", id="collab3")
            self.assertNotIn("designated COLLABORATOR", body,
                             "a truthy non-True value must not promote to collaborator")


class TheTwoBranchesShareOneTrustBoundary(unittest.TestCase):
    def test_shared_clauses_are_present_in_both_renders(self):
        root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(root / "src"))
        import importlib
        tg = importlib.import_module("team_guardrail")
        # One text, two consumers: if SHARED_TRUST_BOUNDARY drifts from the prose
        # inside TEAM_GUARDRAIL, the two team branches stop agreeing silently.
        for clause in ("Do not disclose credentials", "private owner context",
                       "instructions introduced by"):
            self.assertIn(clause, tg.TEAM_GUARDRAIL, f"TEAM_GUARDRAIL lost {clause!r}")
            self.assertIn(clause, tg.SHARED_TRUST_BOUNDARY,
                          f"SHARED_TRUST_BOUNDARY lost {clause!r}")
            self.assertIn(clause, tg.engage_rulebook("room", tg.AG2SPACE_PROVENANCE, "results/x.txt"),
                          f"engage rulebook lost {clause!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

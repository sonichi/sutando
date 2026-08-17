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
    tid = mod._write_task(task)
    assert tid, "writer returned no task id"
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
        a = (root / "src" / "team_guardrail.py").read_text()
        b = (root / "packages" / "ag2-sparrow" / "ag2_sparrow" / "team_guardrail.py").read_text()
        self.assertEqual(a, b, "src/team_guardrail.py and the packaged copy have diverged")


if __name__ == "__main__":
    unittest.main(verbosity=2)

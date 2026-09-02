"""HITL v1 schema + manager contract tests.

The discriminating cases are the stale gates: an action carrying yesterday's
revision or a repainted interaction's guard must raise, never execute.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import (  # noqa: E402
    Action,
    ActionReply,
    HumanRequirement,
    MalformedActionError,
    StaleRequirementError,
    WIRE_FIELD,
    validate_action,
)


def make_req(**kw):
    defaults = dict(
        kind="auth",
        runtime="claude",
        message="Claude Code needs to sign in again",
        guard="screen-hash-abc",
        actions=[Action(id="reauth", kind="authenticate", label="Re-authenticate")],
    )
    defaults.update(kw)
    return HumanRequirement(**defaults)


def reply_for(req, action_id="reauth", **kw):
    defaults = dict(
        hitl_id=req.id, expected_revision=req.revision, action_id=action_id, guard=req.guard
    )
    defaults.update(kw)
    return ActionReply(**defaults)


class SchemaTests(unittest.TestCase):
    def test_wire_shape(self):
        req = make_req(title="t", device={"id": "d1", "name": "Qingyun's Air"})
        w = req.to_wire()
        self.assertEqual(w["kind"], "auth")
        self.assertEqual(w["status"], "pending")
        self.assertEqual(w["revision"], 1)
        self.assertEqual(w["guard"], "screen-hash-abc")
        self.assertEqual(w["actions"][0], {"id": "reauth", "kind": "authenticate", "label": "Re-authenticate"})
        self.assertEqual(w["device"]["name"], "Qingyun's Air")
        self.assertEqual(WIRE_FIELD, "space.ag2.hitl")

    def test_unknown_kind_coerces(self):
        self.assertEqual(make_req(kind="martian").kind, "unknown")

    def test_valid_action_passes(self):
        req = make_req()
        action = validate_action(req, reply_for(req))
        self.assertEqual(action.id, "reauth")

    def test_stale_revision_rejected(self):
        req = make_req()
        old = reply_for(req)
        req.refresh_guard("screen-hash-xyz")  # repaint: revision bumps too
        with self.assertRaises(StaleRequirementError):
            validate_action(req, old)

    def test_stale_guard_rejected_even_at_matching_revision(self):
        # The two layers are independent: hand-craft a reply that matches the
        # current revision but carries the old interaction's guard.
        req = make_req()
        bad = reply_for(req, guard="some-older-guard")
        with self.assertRaises(StaleRequirementError):
            validate_action(req, bad)

    def test_terminal_rejects_actions(self):
        req = make_req()
        req.transition("resolved")
        with self.assertRaises(StaleRequirementError):
            validate_action(req, reply_for(req))

    def test_unknown_action_id(self):
        req = make_req()
        with self.assertRaises(MalformedActionError):
            validate_action(req, reply_for(req, action_id="nope"))

    def test_lifecycle_transitions(self):
        req = make_req()
        req.transition("in_progress")
        self.assertEqual(req.revision, 2)
        req.transition("resolved")
        self.assertEqual(req.revision, 3)
        with self.assertRaises(StaleRequirementError):
            req.transition("pending")

    def test_pending_may_resolve_directly(self):
        req = make_req()
        req.transition("resolved")  # probe cleared it, no click ever came
        self.assertTrue(req.terminal)

    def test_action_reply_from_wire_rejects_garbage(self):
        with self.assertRaises(MalformedActionError):
            ActionReply.from_wire({"hitl_id": "x"})


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = HitlManager(HitlStore(Path(self.tmp.name)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_persist_roundtrip(self):
        req = self.mgr.create(make_req())
        loaded = self.mgr.get(req.id)
        self.assertEqual(loaded.guard, "screen-hash-abc")
        self.assertEqual(loaded.actions[0].kind, "authenticate")

    def test_dedup_same_runtime_kind_refreshes_guard(self):
        a = self.mgr.create(make_req(guard="g1"))
        b = self.mgr.create(make_req(guard="g2"))
        self.assertEqual(a.id, b.id)  # no duplicate card
        self.assertEqual(self.mgr.get(a.id).guard, "g2")
        self.assertEqual(self.mgr.get(a.id).revision, 2)  # old cards went stale

    def test_dedup_is_per_device_so_two_sessions_stay_two_cards(self):
        a = self.mgr.create(make_req(guard="g1", device={"id": "core-1"}))
        b = self.mgr.create(make_req(guard="g1", device={"id": "core-2"}))
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(len(self.mgr.active()), 2)

    def test_apply_action_marks_in_progress(self):
        req = self.mgr.create(make_req())
        action = self.mgr.apply_action(reply_for(req))
        self.assertEqual(action.kind, "authenticate")
        self.assertEqual(self.mgr.get(req.id).status, "in_progress")

    def test_apply_action_stale_leaves_state_untouched(self):
        req = self.mgr.create(make_req())
        with self.assertRaises(StaleRequirementError):
            self.mgr.apply_action(reply_for(req, expected_revision=99))
        self.assertEqual(self.mgr.get(req.id).status, "pending")

    def test_resolve_returns_blocked_tasks(self):
        req = self.mgr.create(make_req())
        self.mgr.link_blocked_task(req.id, "task-123")
        self.mgr.link_blocked_task(req.id, "task-123")  # idempotent
        self.mgr.link_blocked_task(req.id, "task-456")
        resumed = self.mgr.resolve(req.id)
        self.assertEqual(resumed, ["task-123", "task-456"])
        self.assertEqual(self.mgr.resolve(req.id), [])  # second resolve: no-op

    def test_projection_ledger_idempotent(self):
        req = self.mgr.create(make_req())
        self.assertTrue(self.mgr.needs_projection(req.id))
        self.mgr.record_projection(req.id, 1, "$event1")
        self.assertFalse(self.mgr.needs_projection(req.id))
        # A retry of an old revision changes nothing.
        self.mgr.record_projection(req.id, 1, "$event-dup")
        self.assertEqual(self.mgr.projection_target(req.id), "$event1")
        # A transition re-opens the need; the EDIT target stays the CREATE event.
        self.mgr.resolve(req.id)
        self.assertTrue(self.mgr.needs_projection(req.id))
        self.mgr.record_projection(req.id, self.mgr.get(req.id).revision, None)
        self.assertEqual(self.mgr.projection_target(req.id), "$event1")
        self.assertFalse(self.mgr.needs_projection(req.id))




class EdgeBranchTests(unittest.TestCase):
    """The guard branches the happy paths never touch — each one is what
    stands between a missing/terminal/corrupt record and a wrong action."""

    def test_transition_with_guard_swaps_it(self):
        req = make_req()
        req.transition("in_progress", guard="g-new")
        self.assertEqual(req.guard, "g-new")

    def test_refresh_guard_on_terminal_raises(self):
        req = make_req()
        req.transition("resolved")
        with self.assertRaises(StaleRequirementError):
            req.refresh_guard("g2")

    def test_action_for_wrong_requirement_id(self):
        req = make_req()
        with self.assertRaises(MalformedActionError):
            validate_action(req, reply_for(req, hitl_id="hitl_other"))


class ManagerEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from hitl.manager import HitlStore

        self.store = HitlStore(Path(self.tmp.name))
        self.mgr = HitlManager(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.mgr.get("hitl_nope"))

    def test_all_skips_corrupt_file(self):
        self.mgr.create(make_req())
        (Path(self.tmp.name) / "hitl_corrupt.json").write_text("{not json")
        (Path(self.tmp.name) / "hitl_nokey.json").write_text("{}")
        self.assertEqual(len(self.store.all()), 1)

    def test_apply_action_on_missing_requirement(self):
        with self.assertRaises(MalformedActionError):
            self.mgr.apply_action(
                ActionReply(hitl_id="hitl_ghost", expected_revision=1, action_id="x", guard="")
            )

    def test_cancel_and_expire_paths(self):
        a = self.mgr.create(make_req())
        self.assertEqual(self.mgr.cancel(a.id), [])
        self.assertEqual(self.mgr.get(a.id).status, "cancelled")
        b = self.mgr.create(make_req(kind="billing"))
        self.mgr.link_blocked_task(b.id, "t1")
        self.assertEqual(self.mgr.expire(b.id), ["t1"])
        self.assertEqual(self.mgr.get(b.id).status, "expired")

    def test_terminate_missing_and_already_terminal(self):
        self.assertEqual(self.mgr.resolve("hitl_ghost"), [])
        req = self.mgr.create(make_req())
        self.mgr.resolve(req.id)
        self.assertEqual(self.mgr.cancel(req.id), [])  # terminal stays terminal

    def test_link_blocked_task_on_missing_requirement_is_noop(self):
        self.mgr.link_blocked_task("hitl_ghost", "t1")  # must not raise

    def test_projection_ops_on_missing_requirement(self):
        self.assertFalse(self.mgr.needs_projection("hitl_ghost"))
        self.mgr.record_projection("hitl_ghost", 1, "$e")  # must not raise


class DefaultRunnerTest(unittest.TestCase):
    def test_default_runner_runs_a_real_command(self):
        from hitl.detector import _default_runner

        rc, out = _default_runner(["echo", "hitl-runner-probe"])
        self.assertEqual(rc, 0)
        self.assertIn("hitl-runner-probe", out)


if __name__ == "__main__":
    unittest.main()

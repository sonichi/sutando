"""HITL v1 schema + manager contract tests.

The discriminating cases are the stale gates: an action carrying yesterday's
revision or a repainted interaction's guard must raise, never execute.
"""

import sys
import tempfile
import unittest
from unittest import mock
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
        req = make_req(title="t", device={"id": "d1", "name": "the owner's laptop"})
        w = req.to_wire()
        self.assertEqual(w["kind"], "auth")
        self.assertEqual(w["status"], "pending")
        self.assertEqual(w["revision"], 1)
        self.assertEqual(w["guard"], "screen-hash-abc")
        self.assertEqual(w["actions"][0], {"id": "reauth", "kind": "authenticate", "label": "Re-authenticate"})
        self.assertEqual(w["device"]["name"], "the owner's laptop")
        self.assertEqual(WIRE_FIELD, "space.ag2.hitl")

    def test_the_wire_subject_carries_only_the_card_fields(self):
        req = make_req(subject={"tool": "Bash", "input": "ls", "options": ["a", "b"],
                                "choices": {"a": "do a"}, "ask_id": "ask-1",
                                "claim": {"token": "t0k", "state": "handed", "handed_at": 1.5,
                                          "lease": {"observed_at": 1.5, "elapsed": 0.25}},
                                "claude-local": {"stall_token": "s"}, "session": "sess",
                                "driven_at": 2.5, "option_for_action": {"a": 1}})
        self.assertEqual(req.to_wire()["subject"], {"tool": "Bash", "input": "ls", "options": ["a", "b"],
                                                    "choices": {"a": "do a"}})
        self.assertIn("claim", req.subject, "the local record keeps every field")

    def test_a_subject_with_no_card_field_leaves_no_subject_on_the_wire(self):
        self.assertNotIn("subject", make_req(subject={"claim": {"token": "t"}}).to_wire())

    def test_every_card_names_the_host_at_the_wire(self):
        # The auth card (detector.auth_requirement) takes its device from a caller that
        # knows only the session; the wire is where every card gets the machine.
        import hitl.schema as SCHEMA
        from hitl.detector import auth_requirement
        with mock.patch.object(SCHEMA, "device_host", return_value="Chis-MacBook-Pro"):
            w = auth_requirement("g1", {"id": "sutando-core", "name": "sutando-core"}).to_wire()
            self.assertEqual(w["device"], {"id": "sutando-core", "name": "sutando-core", "host": "Chis-MacBook-Pro"})
            kept = make_req(device={"id": "d1", "name": "n", "host": "Other-Mac"}).to_wire()["device"]
            self.assertEqual(kept["host"], "Other-Mac")  # a producer's own value is never overwritten
            self.assertNotIn("device", make_req(device=None).to_wire())  # no device, no invented one
        with mock.patch.object(SCHEMA, "device_host", return_value=""):
            self.assertNotIn("host", make_req(device={"id": "d1"}).to_wire()["device"])  # unknown: no key, not ""
        calls = []
        def once():
            calls.append(1)
            return "Chis-MacBook-Pro" if len(calls) == 1 else ""
        with mock.patch.object(SCHEMA, "device_host", side_effect=once):
            self.assertEqual(make_req(device={"id": "d1"}).to_wire()["device"].get("host"), "Chis-MacBook-Pro")
        self.assertEqual(len(calls), 1)

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

    def test_the_third_layer_pins_a_reply_to_the_requirements_room(self):
        """When the requirement names a room, a reply from any other room is stale."""
        req = make_req(room="!owner-dm:ag2.space")
        reply = reply_for(req)
        with self.assertRaises(StaleRequirementError) as e:
            validate_action(req, reply)
        self.assertIn("another room", str(e.exception))
        reply.room = "!other:ag2.space"
        with self.assertRaises(StaleRequirementError):
            validate_action(req, reply)
        reply.room = "!owner-dm:ag2.space"
        self.assertEqual(validate_action(req, reply).id, "reauth")
        anywhere = make_req()
        self.assertEqual(anywhere.room, "", "a requirement with no room takes a reply from anywhere")
        self.assertEqual(validate_action(anywhere, reply_for(anywhere, room="!x:y")).id, "reauth")
        wire = ActionReply.from_wire({"hitl_id": req.id, "expected_revision": 1, "action_id": "reauth",
                                      "guard": req.guard, "room": "!owner-dm:ag2.space", "sender": "@chi:ag2.space"})
        self.assertEqual((wire.room, wire.sender), ("!owner-dm:ag2.space", "@chi:ag2.space"))
        bare = ActionReply.from_wire({"hitl_id": req.id, "expected_revision": 1, "action_id": "reauth", "guard": "g"})
        self.assertEqual((bare.room, bare.sender), ("", ""))

    def test_action_reply_from_wire_rejects_garbage(self):
        with self.assertRaises(MalformedActionError):
            ActionReply.from_wire({"hitl_id": "x"})


def _is_dir_fd(fd):
    import os as _os
    import stat as _stat
    return _stat.S_ISDIR(_os.fstat(fd).st_mode)


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
        self.assertEqual(self.mgr.get(req.id).chosen_action, "reauth")

    def test_apply_action_runs_the_patch_after_the_reload_inside_the_one_save(self):
        """`patch(req)` runs after the reload and before the sole save, so what it sets lands
        in the same write as the acceptance; the production save count is exactly one."""
        req = self.mgr.create(make_req(kind="choice"))
        saves = []
        real = HitlStore.save

        def counting(store, r, projection=None):
            saves.append((r.id, r.status, dict(r.subject)))
            return real(store, r, projection)
        seen = []

        def patch(r):
            seen.append((r.status, r.chosen_action, r.revision))
            r.subject["claude-local"] = {"effect_phase": "STOPPING", "stall_token": "t1"}
        with mock.patch.object(HitlStore, "save", counting):
            action = self.mgr.apply_action(reply_for(req), patch=patch)
        self.assertEqual(action.id, "reauth")
        self.assertEqual(seen, [("in_progress", "reauth", 2)], "the patch sees the accepted, reloaded requirement")
        self.assertEqual(saves, [(req.id, "in_progress", {"claude-local": {"effect_phase": "STOPPING",
                                                                            "stall_token": "t1"}})],
                         "exactly one save, carrying the patch")
        stored = self.mgr.get(req.id)
        self.assertEqual((stored.status, stored.chosen_action, stored.subject["claude-local"]["effect_phase"]),
                         ("in_progress", "reauth", "STOPPING"))
        stale = self.mgr.create(make_req(kind="choice", guard="g2"))
        with self.assertRaises(StaleRequirementError):
            self.mgr.apply_action(reply_for(stale, expected_revision=9), patch=lambda r: r.subject.update(never="written"))
        self.assertNotIn("never", self.mgr.get(stale.id).subject, "a refused action runs no patch")

    def test_save_fsyncs_the_store_directory_after_the_replace_and_raises_when_it_fails(self):
        """The record is durable, or the caller hears that it may not be."""
        req = make_req()
        import os as _os
        real_fsync, real_replace = _os.fsync, _os.replace
        order = []
        with mock.patch.object(_os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1]), \
                mock.patch.object(_os, "fsync", lambda fd: (order.append("dir" if _is_dir_fd(fd) else "file"),
                                                            real_fsync(fd))[1]):
            self.mgr.store.save(req)
        self.assertEqual(order, ["file", "replace", "dir"], "file fsync, replace, then the directory fsync")
        real = _os.fsync
        with mock.patch.object(_os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "fsync failed"))
                               if _is_dir_fd(fd) else real(fd)):
            with self.assertRaises(OSError):
                self.mgr.store.save(req)
        self.assertIsNotNone(self.mgr.store.load(req.id), "the replace stood; only its durability is in doubt")

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

        rc, out = _default_runner(
            [sys.executable, "-c", "print('hitl-runner-probe')"])
        self.assertEqual(rc, 0)
        self.assertIn("hitl-runner-probe", out)


class DefaultPathsTest(unittest.TestCase):
    def test_no_workspace_resolves_through_the_repo_resolver_imported_at_the_call(self):
        """The resolver is imported only when no workspace is given, so a vendored copy never needs it."""
        import types
        from hitl import manager, replies
        ws = Path(tempfile.mkdtemp(prefix="hitl-ws-"))
        fake = types.ModuleType("workspace_default")
        fake.resolve_workspace = lambda *a, **kw: ws
        with mock.patch.dict(sys.modules, {"workspace_default": fake}):
            self.assertEqual(manager.default_store(), ws / "state" / "hitl" / "requirements")
            self.assertEqual(replies.actions_dir(), ws / "state" / "hitl" / "actions")
        self.assertEqual(manager.default_store(ws / "x"), ws / "x" / "state" / "hitl" / "requirements")


if __name__ == "__main__":
    unittest.main()

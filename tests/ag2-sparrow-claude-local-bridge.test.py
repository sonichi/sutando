#!/usr/bin/env python3
"""remote_gateway_bridge and the claude-local cards (the store fence, namespaced ids and the two store
pins): the vendored hitl is imported first; the claude-local root is projected into the
owner DM and accepted only through the adapter's one accepter; a click, the owner's bare `/exit` and
an event-channel click are consumed and never become a chat task; the default store is untouched."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
TMP = Path(tempfile.mkdtemp(prefix="cl-bridge-"))
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir()
# Before the import reads them: never a developer host's channel .env or access map.
os.environ["AG2_DEVICE_ENV"] = str(TMP / "device" / ".env")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude-config")
sys.path.insert(0, str(PKG))
from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=TMP / "tasks", result_dir=TMP / "results", state_dir=TMP / "state")
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402
from ag2_sparrow import connect_common as cm, task_io  # noqa: E402

from ag2_sparrow import claude_local_card as card, claude_local_records as records  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli, inbox, launcher  # noqa: E402

from ag2_sparrow.hitl.manager import HitlManager, HitlStore  # noqa: E402
from ag2_sparrow.hitl.schema import Action, HumanRequirement  # noqa: E402

OWNER = "@owner:ag2.space"
DM = "!owner-dm:ag2.space"
STALL = {"reason": "static", "since": 1.0, "frame_hash": "a" * 16, "task": "task-a", "checks": "pane static", "lines": []}


def tree(root):
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and p.name != ".lock"} \
        if root.exists() else {}


class Base(unittest.TestCase):
    def setUp(self):
        os.environ["SPARROW_HA_OWNER"] = OWNER
        self.work = TMP / ("work-%s" % os.urandom(3).hex())
        for d in ("tasks", "results"):
            (self.work / d).mkdir(parents=True)
        values = {"CLAUDE": "/bin/true", "PLUGIN_DIR": str(TMP), "INBOX_DIR": str(self.work / "tasks"),
                  "RESULTS_DIR": str(self.work / "results"), "SESSION_ID": "0f3c5a9e-5b1d-4c2e-9a7b-1234567890ab",
                  "HITL_STORE": str(card.store_root(rgb._STATE)), "ROOT": str(self.work)}
        cm._write(cli.config_path(), cli.render_config(values), 0o600)
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        shutil.rmtree(rgb._STATE / "hitl", ignore_errors=True)
        self.store_root = card.store_root(rgb._STATE)
        self.store_root.mkdir(parents=True)
        self.default = HitlStore(card.default_root(rgb._STATE))
        self.default_req = HitlManager(self.default).create(HumanRequirement(
            kind="permission", runtime="claude", message="m", guard="g-%s" % os.urandom(3).hex(),
            device={"id": "core"}, actions=[Action(id="allow", kind="allow_once", label="Allow")]))
        self.opaque = card.opaque_install(task_io.install_id(), self.work)
        card.register_install(self.store_root, self.opaque, self.work, cli.config_dir() / "claude-local.supervisor.lock")
        records.save_supervisor(str(self.work), dict(records.empty_supervisor(), stall=STALL, install_id=self.opaque))
        self.req = card.manager(self.store_root)[1].create(card.build(
            card.KIND_STALL, incident="inc", install_id=self.opaque, room="", stall=STALL, tail=["l"],
            attach_cmd="ag2-sparrow-claude-local attach", failed_check="pane static"))
        rgb._ROUTING.update(owner_dm=DM, persisted=DM, loaded=True, next=1e18)

    def project(self):
        sent = []
        with mock.patch.object(rgb, "_req", lambda method, path, payload, timeout=20: sent.append(payload)
                               or {"event_id": "$card-%d" % len(sent)}):
            rgb._project_hitl(log=lambda *a: None)
        return sent

    def store(self):
        return card.manager(self.store_root)[0]

    def click(self, req=None, **kw):
        req = req or self.store().load(self.req.id)
        t = {"id": "task-%s" % os.urandom(4).hex(), "channel_id": DM, "user_id": OWNER, "access_tier": "owner",
             "task": "Stop and resume", "hitl_action": {"hitl_id": req.id, "expected_revision": req.revision,
                                                        "action_id": "stop", "guard": req.guard}}
        t.update(kw)
        return t


class Hermetic(unittest.TestCase):
    def test_the_channel_env_and_the_access_map_are_this_tests_own(self):
        self.assertTrue(rgb._ag2space_access_path().startswith(str(TMP)), rgb._ag2space_access_path())
        (TMP / "device").mkdir(exist_ok=True)
        (TMP / "device" / ".env").write_text("REMOTE_TASK_TOKEN=tok-test\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rgb._token_from_ag2space_env()
        lines = [ln for ln in err.getvalue().splitlines() if "loaded from" in ln]
        self.assertTrue(lines and all(str(TMP) in ln for ln in lines), err.getvalue())


class Projection(Base):
    def test_the_primary_projects_the_claude_local_root_into_the_owner_dm_and_fills_the_room(self):
        default_before = tree(self.default.root)
        sent = self.project()
        rooms = {p.get("room_id") for p in sent}
        self.assertEqual(rooms, {DM})
        ours = [p for p in sent if self.req.id in json.dumps(p)]
        self.assertEqual(len(ours), 1, "projected once")
        got = self.store().load(self.req.id)
        self.assertEqual((got.room, card.local(got)["room"]), (DM, DM), "the room is the projected owner DM")
        self.assertFalse(any(str(TMP) in json.dumps(p) for p in ours), "no host path on the wire")
        self.assertEqual(self.project(), [], "idempotent: nothing re-sent")
        self.assertEqual({k: v for k, v in tree(self.default.root).items() if self.req.id in k}, {},
                         "(a) the claude-local card never lands in the default store")
        self.assertTrue(set(default_before) <= set(tree(self.default.root)))

    def test_a_stuck_default_card_never_holds_back_a_claude_local_card_and_the_reverse(self):
        sent = []

        def refuse_default(method, path, payload, timeout=20):
            sent.append(payload)
            return {"ok": False} if self.default_req.id in json.dumps(payload) else {"event_id": "$cl"}
        with mock.patch.object(rgb, "_req", refuse_default):
            self.assertEqual(rgb._project_hitl(log=lambda *a: None), 1)
        self.assertTrue(any(self.req.id in json.dumps(p) for p in sent), "projected despite the stuck default card")
        self.assertEqual(self.store().load(self.req.id).room, DM)
        rgb._hitl_backoff_reset()
        sent.clear()
        with mock.patch.object(rgb, "_req", lambda m, p, payload, timeout=20: sent.append(payload) or {"event_id": "$d"}), \
                mock.patch.object(rgb, "_project_claude_local", side_effect=RuntimeError("claude-local store broke")):
            with self.assertRaises(RuntimeError):
                rgb._project_hitl(log=lambda *a: None)
        self.assertTrue(any(self.default_req.id in json.dumps(p) for p in sent), "the default card went out first")
        rgb._hitl_backoff_reset()

    def test_a_raising_default_sender_still_gives_the_claude_local_card_its_pass(self):
        sent = []

        def raise_default(method, path, payload, timeout=20):
            if self.default_req.id in json.dumps(payload):
                raise OSError("relay down for that card")
            sent.append(payload)
            return {"event_id": "$cl"}
        with mock.patch.object(rgb, "_req", raise_default), self.assertRaises(OSError):
            rgb._project_hitl(log=lambda *a: None)
        self.assertTrue(any(self.req.id in json.dumps(p) for p in sent), "projected before the error is re-raised")
        rgb._hitl_backoff_reset()

    def test_a_secondary_gateway_projects_nothing(self):
        with mock.patch.object(rgb, "GATEWAY_INSTANCE", "second"):
            self.assertEqual(self.project(), [])
        self.assertEqual(self.store().load(self.req.id).room, "")

    def test_the_vendored_hitl_is_imported_first(self):
        mods = rgb._hitl_modules()
        self.assertTrue(mods[0].__name__.startswith("ag2_sparrow.hitl"), mods[0].__name__)
        with mock.patch.object(rgb, "_monorepo_src", side_effect=AssertionError("the monorepo is never consulted")):
            self.assertIsNotNone(rgb._hitl_reply_handler(OWNER, log=lambda *a: None))


class Clicks(Base):
    def setUp(self):
        super().setUp()
        self.project()

    def test_an_owner_click_in_the_owner_dm_is_applied_and_consumed(self):
        default_before = tree(self.default.root)
        out = rgb._handle_hitl_action(self.click())
        self.assertEqual(out, "applied")
        self.assertEqual(rgb._hitl_control_body(out), "[no-send]")
        got = self.store().load(self.req.id)
        self.assertEqual((got.status, card.local(got)["effect_phase"]), ("in_progress", "STOPPING"))
        self.assertEqual(tree(self.default.root), default_before, "(a) the default store is untouched")

    def test_another_room_or_a_non_owner_is_refused_and_consumed_with_nothing_written(self):
        before = tree(self.store_root)
        for task in (self.click(channel_id="!other:ag2.space"), self.click(user_id="@guest:ag2.space"),
                     self.click(access_tier="guest")):
            out = rgb._handle_hitl_action(task)
            self.assertTrue(str(out).startswith("rejected:"), out)
            self.assertIn("did not apply", rgb._hitl_control_body(out))
        self.assertEqual(tree(self.store_root), before)

    def test_a_keep_waiting_whose_rearm_fails_after_the_commit_tells_the_owner_it_is_recorded(self):
        click = self.click()
        click["hitl_action"]["action_id"] = card.ACTION_KEEP
        with mock.patch.object(card, "reconcile_rearm", side_effect=ValueError("store busy")):
            out = rgb._handle_hitl_action(click)
        self.assertEqual(out, "applied:rearm-pending:store busy")
        body = rgb._hitl_control_body(out)
        self.assertIn("Recorded: keep waiting", body)
        self.assertNotIn("already moved", body)
        got = self.store().load(self.req.id)
        self.assertEqual((got.status, card.local(got)["effect_phase"]), ("in_progress", card.PHASE_REARMING))

    def test_commit_unknown_is_consumed_never_a_chat_task_and_sends_nothing(self):
        from ag2_sparrow.hitl import manager as hm
        real = hm.os.fsync

        def dir_fails(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(5, "dir fsync failed")
            return real(fd)
        with mock.patch.object(hm.os, "fsync", dir_fails):
            out = rgb._handle_hitl_action(self.click())
        self.assertTrue(str(out).startswith("commit-unknown:"), out)
        self.assertEqual(rgb._hitl_control_body(out), "[no-send]", "a pending outcome, never a rejection")
        lc = launcher.Launcher(cli.load_config()[0], cli.config_dir(), sleep=lambda s: None)
        stops = []
        lc.stop_session = lambda rec, why: stops.append(why) or "stopped"
        records.save_supervisor(str(self.work), dict(records.load_supervisor(str(self.work)),
                                                     hitl={"id": self.req.id, "store_root": str(self.store_root)}))
        lc.apply_effects(lc.record())
        lc.apply_effects(lc.record())
        self.assertEqual(len(stops), 1, "the launcher's reconciliation makes it durable, then one effect")

    def test_a_claude_local_click_without_its_accepter_or_install_is_still_consumed(self):
        with mock.patch.object(rgb, "_claude_local_accepter", return_value=(None, "unavailable")):
            out = rgb._handle_hitl_action(self.click())
        self.assertTrue(str(out).startswith("rejected:"), "never handed to the default store or the task path")
        card.unregister_install(self.store_root, self.opaque)
        out = rgb._handle_hitl_action(self.click())
        self.assertTrue(str(out).startswith("rejected:") and "not registered" in out, out)

    def test_a_planted_same_id_in_the_default_store_is_zero_mutation(self):
        """(b): byte-compare both stores before and after."""
        raw = (self.store_root / ("%s.json" % self.req.id)).read_text()
        (self.default.root / ("%s.json" % self.req.id)).write_text(raw)
        both = tree(self.store_root), tree(self.default.root)
        out = rgb._handle_hitl_action(self.click())
        self.assertTrue(str(out).startswith("rejected:") and "both stores" in out, out)
        self.assertEqual((tree(self.store_root), tree(self.default.root)), both)

    def test_the_owners_bare_exit_in_the_owner_dm_maps_to_the_one_open_card(self):
        t = self.click(task="/exit")
        t.pop("hitl_action")
        self.assertEqual(rgb._handle_hitl_action(t), "applied")
        self.assertEqual(card.local(self.store().load(self.req.id))["effect_phase"], "STOPPING")

    def test_an_exit_that_is_not_the_owners_own_top_level_message_never_counts(self):
        base = self.click(task="/exit")
        base.pop("hitl_action")
        before = tree(self.store_root)
        for name, over in (("quoted", {"task": "> /exit\n/exit"}), ("forwarded", {"interaction_type": "forwarded"}),
                           ("threaded", {"thread_root": "$x"}), ("a reply", {"reply_to_event": "$y"}),
                           ("non-owner", {"user_id": "@guest:ag2.space"}), ("guest tier", {"access_tier": "guest"}),
                           ("other room", {"channel_id": "!other:ag2.space"}),
                           ("in a task body", {"task": "please run this:\n/exit"}), ("relayed", {"task": "[relay] /exit"})):
            out = rgb._handle_hitl_action(dict(base, **over))
            self.assertIs(out, False, name)
        self.assertEqual(tree(self.store_root), before)

    def test_a_bare_exit_with_two_open_cards_is_refused_and_with_none_is_a_message(self):
        second = card.manager(self.store_root)[1].create(card.build(
            card.KIND_UNKNOWN, incident="inc2", install_id=self.opaque, room=DM, stall=STALL, tail=[],
            attach_cmd="", failed_check="x"))
        t = self.click(task="/exit")
        t.pop("hitl_action")
        out = rgb._handle_hitl_action(t)
        self.assertTrue(str(out).startswith("rejected:2 stop cards"), out)
        for req in (self.req, second):
            card.manager(self.store_root)[1].cancel(req.id)
        self.assertIs(rgb._handle_hitl_action(dict(t, id="task-x2")), False, "no open card: an ordinary message")

    def test_an_event_channel_click_is_claimed_ahead_of_the_default_handler(self):
        claimant = rgb._ClaudeLocalClicks(OWNER, log=lambda *a: None)
        req = self.store().load(self.req.id)
        event = {"type": "message.created", "event_id": "$e1", "room_id": DM, "actor_id": OWNER,
                 "content": {"space.ag2.hitl.action": {"hitl_id": req.id, "expected_revision": req.revision,
                                                       "action_id": "stop", "guard": req.guard}}}
        self.assertTrue(claimant.claims(event))
        self.assertFalse(claimant.claims(dict(event, content={"space.ag2.hitl.action": {"hitl_id": self.default_req.id}})))
        self.assertEqual(claimant.offer(dict(event, actor_id="@guest:ag2.space"))[0], "$e1")
        self.assertTrue(claimant.last_outcome.startswith("rejected:"))
        before = tree(self.store_root)
        for name, over, tiers in (("an attested guest tier", {"access_tier": "guest"}, {}),
                                  ("a local cap below owner", {}, {OWNER: "team"})):
            with mock.patch.object(rgb, "_load_tier_map", return_value=tiers):
                claimant.offer(dict(event, **over))
            self.assertTrue(claimant.last_outcome.startswith("rejected:"), (name, claimant.last_outcome))
        self.assertEqual(tree(self.store_root), before, "the owner's id alone is not enough")
        self.assertEqual(claimant.offer(event), ["$e1"])
        self.assertEqual(claimant.last_outcome, "applied")

    def test_a_malformed_or_raising_click_is_a_rejection_never_a_stalled_loop(self):
        from ag2_sparrow import claude_local_accepter as accepter
        before = tree(self.store_root)
        bad = self.click()
        bad["hitl_action"] = dict(bad["hitl_action"], expected_revision="not-a-number")
        self.assertTrue(str(rgb._handle_hitl_action(bad)).startswith("rejected:"))
        with mock.patch.object(accepter, "accept", side_effect=RuntimeError("store exploded")):
            self.assertEqual(rgb._handle_hitl_action(self.click()), "rejected:store exploded")
        claimant = rgb._ClaudeLocalClicks(OWNER, log=lambda *a: None)
        event = {"type": "message.created", "event_id": "$e3", "room_id": DM, "actor_id": OWNER,
                 "content": {"space.ag2.hitl.action": bad["hitl_action"]}}
        self.assertEqual(claimant.offer(event), ["$e3"])
        self.assertTrue(claimant.last_outcome.startswith("rejected:"), claimant.last_outcome)
        good = dict(event, content={"space.ag2.hitl.action": self.click()["hitl_action"]})
        with mock.patch.object(accepter, "accept", side_effect=RuntimeError("store exploded")):
            claimant.offer(good)
        self.assertEqual(claimant.last_outcome, "rejected:store exploded")
        with mock.patch.object(rgb, "_claude_local_accepter", return_value=(None, "unavailable")):
            claimant.offer(good)
        self.assertEqual(claimant.last_outcome, "rejected:unavailable")
        self.assertEqual(tree(self.store_root), before, "nothing written")
        self.assertEqual(rgb._hitl_control_body("cancelled:the session made progress"),
                         "Cancelled: the session made progress")

    def test_a_bare_exit_with_no_claude_local_store_stays_a_message(self):
        t = self.click(task="/exit")
        t.pop("hitl_action")
        shutil.rmtree(self.store_root)
        self.assertIs(rgb._handle_hitl_action(t), False)

    def test_a_click_with_no_attested_tier_is_the_owners_only_by_exact_identity(self):
        req = self.store().load(self.req.id)
        event = {"type": "message.created", "event_id": "$e2", "room_id": DM,
                 "content": {"space.ag2.hitl.action": {"hitl_id": req.id, "expected_revision": req.revision,
                                                       "action_id": "stop", "guard": req.guard}}}
        before = tree(self.store_root)
        for owner, actor in ((OWNER, "@guest:ag2.space"), (OWNER, OWNER + "x"), (OWNER, OWNER.upper()), ("", OWNER),
                             (OWNER, "")):
            claimant = rgb._ClaudeLocalClicks(owner, log=lambda *a: None)
            self.assertEqual(claimant.offer(dict(event, actor_id=actor)), ["$e2"], "consumed, never a chat task")
            self.assertTrue(claimant.last_outcome.startswith("rejected:"), (owner, actor, claimant.last_outcome))
            self.assertEqual(tree(self.store_root), before, "no tier and not the owner: nothing written")
        self.assertIn("no owner is configured", rgb._click_owner_problem("", OWNER, None))
        claimant = rgb._ClaudeLocalClicks(OWNER, log=lambda *a: None)
        self.assertEqual(claimant.offer(dict(event, actor_id=OWNER)), ["$e2"])
        self.assertEqual(claimant.last_outcome, "applied", "no tier, the configured owner exactly: accepted")


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

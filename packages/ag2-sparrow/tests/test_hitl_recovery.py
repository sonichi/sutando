"""The recovery click grammar the bridge's reply handler uses (`ag2_sparrow.hitl.recovery`, vendored
from `src/hitl/recovery.py`): which ids are recovery clicks, their labels, and the hand-off and
receipt files. Every case runs against the vendored copy, and against the src/ copy when this
package sits in its repository.

Run:
    python3 packages/ag2-sparrow/tests/test_hitl_recovery.py
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow.hitl import recovery as vendored

COPIES = [("vendored", vendored)]
_SRC = PKG_ROOT.parents[1] / "src" / "hitl" / "recovery.py"
if _SRC.is_file():
    _spec = importlib.util.spec_from_file_location("src_hitl_recovery", _SRC)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    COPIES.append(("src", _mod))


class Grammar(unittest.TestCase):
    def test_the_allowlisted_ids_parse_and_nothing_else_does(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                for aid, want in (("retry_now", ("retry_now", None)), ("refresh_options", ("refresh_options", None)),
                                  ("switch_model:o4b876be2aaa6", ("switch_model", "o4b876be2aaa6")),
                                  ("switch_runtime:codex", ("switch_runtime", "codex")),
                                  ("switch_account:acct_3f9a2b", ("switch_account", "acct_3f9a2b"))):
                    self.assertEqual(rec.parse(aid), want, aid)
                    self.assertTrue(rec.is_recovery(aid))
                for bad in (None, 3, "", "rm -rf /", "switch_model:", "switch_model:Opus", "switch_model:a b",
                            "switch_model:sonnet-5-1m-context", "switch_model:claude-opus-5-5", "switch_model:Opus 5.5",
                            "switch_model:o4b876be2aaa6\n", "switch_model:o4b876be2aaa", "retry_now\n",
                            "switch_runtime:../x", "switch_account:me@example.com", "open_terminal", "opt1"):
                    self.assertIsNone(rec.parse(bad), bad)
                    self.assertFalse(rec.is_recovery(bad))

    def test_ids_are_built_only_from_the_grammar(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.action_id("switch_model", "o4b876be2aaa6"), "switch_model:o4b876be2aaa6")
                self.assertEqual(rec.action_id("restart_core"), "restart_core")
                with self.assertRaises(ValueError):
                    rec.action_id("switch_model", "Bad Key")

    def test_labels_name_what_the_runtime_calls_each_choice(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.label("switch_model:o4b876be2aaa6", model_names={"o4b876be2aaa6": "Haiku 4.5"}),
                                 "Switch to Haiku 4.5")
                self.assertEqual(rec.label("switch_model:o4b876be2aaa6"), "Switch model")
                self.assertEqual(rec.label("switch_runtime:codex", {"codex": "Codex"}), "Switch to Codex")
                self.assertEqual(rec.label("switch_runtime:codex"), "Switch to codex")
                self.assertEqual(rec.label("switch_account:acct_3f9a2b"), "Switch to account acct_3f9a2b")
                self.assertEqual(rec.label("refresh_options"), "Refresh options")
                with self.assertRaises(ValueError):
                    rec.label("nope")

    def test_only_the_purchase_is_tagged_apart(self):
        for name, rec in COPIES:
            with self.subTest(copy=name):
                self.assertEqual(rec.kind_of(rec.BUY_CREDITS), rec.PURCHASE_KIND)
                self.assertEqual(rec.kind_of(rec.RETRY_NOW), rec.ACTION_KIND)


class Files(unittest.TestCase):
    def test_a_click_is_handed_off_and_its_receipt_read_back(self):
        for name, rec in COPIES:
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                reply = SimpleNamespace(hitl_id="hr_01J9Z8K2", expected_revision=3,
                                        action_id="switch_model:o4b876be2aaa6", guard="g")
                path = rec.write_action(ws, reply, "@owner:s.space", "!dm:s.space", now=7)
                self.assertEqual(path, rec.actions_dir(ws) / "hr_01J9Z8K2.json")
                body = json.loads(path.read_text())
                self.assertEqual((body["action_id"], body["sender"], body["at"]),
                                 ("switch_model:o4b876be2aaa6", "@owner:s.space", 7))
                with self.assertRaises(ValueError):
                    rec.write_action(ws, SimpleNamespace(hitl_id="x", expected_revision=1, action_id="rm",
                                                         guard="g"), "s", "r")
                got = rec.receipt("hr_01J9Z8K2", "inc_0123abcd", "switch_model:haiku", "done", None, 9.5,
                                  {"state": "running"})
                self.assertEqual(got["at"], 9)
                rec.write_receipt(ws, got)
                self.assertEqual(rec.read_receipt(ws, "hr_01J9Z8K2"), got)
                self.assertIsNone(rec.read_receipt(ws, "hr_missing"))
                (rec.receipts_dir(ws) / "hr_list.json").write_text("[1]")
                self.assertIsNone(rec.read_receipt(ws, "hr_list"), "a non-object receipt is no receipt")
                for bad in ({**got, "outcome": "maybe"},):
                    with self.assertRaises(ValueError):
                        rec.write_receipt(ws, bad)
                with self.assertRaises(ValueError):
                    rec.receipt("h", "i", "retry_now", "maybe", None, 1)

    def test_an_id_is_one_path_component(self):
        for name, rec in COPIES:
            with self.subTest(copy=name), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                path = rec.write_receipt(ws, rec.receipt("hitl_3d12-a_b", "i", "retry_now", "done", None, 1))
                self.assertEqual((path.parent, path.name), (rec.receipts_dir(ws), "hitl_3d12-a_b.json"))
                # Rejected, not stripped: "a.b" never names the same file as "ab".
                for bad in ("../../etc/x", "/..", "a.b", "a b", "é"):
                    with self.subTest(bad=bad), self.assertRaises(ValueError):
                        rec.write_receipt(ws, rec.receipt(bad, "i", "retry_now", "done", None, 1))



class VendoredCardAnswers(unittest.TestCase):
    """The vendored reply path answers a recovery tap on its card: the tap's own time is kept, a tap
    from another room or on an old revision runs nothing, and a rejected tap's card says `stale`."""

    def setUp(self):
        from ag2_sparrow.hitl.manager import HitlManager, HitlStore
        from ag2_sparrow.hitl.replies import HitlReplyHandler
        from ag2_sparrow.hitl.schema import HumanRequirement, Action
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        self.req = self.mgr.create(HumanRequirement(
            kind="core-blocked", runtime="claude", message="m", guard="limit:core",
            actions=[Action(id="restart_core", label="Restart core", kind=vendored.ACTION_KIND)],
            subject={"recovery": True, "session": "core"}))
        self.mgr.record_projection(self.req.id, self.req.revision, "$card", {"room_id": "!dm:s.space"})
        self.handler = HitlReplyHandler(self.mgr, "@owner:s.space", workspace=self.ws, log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def tap(self, room="!dm:s.space", revision=None, **extra):
        from ag2_sparrow.hitl.replies import REPLY_FIELD
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": "@owner:s.space",
                            "room_id": room, **extra, "content": {REPLY_FIELD: {
                                "hitl_id": self.req.id, "expected_revision": revision or self.req.revision,
                                "action_id": "restart_core", "guard": self.req.guard}}})
        return self.handler.last_outcome

    def test_the_taps_own_time_rides_on_the_hand_off(self):
        self.assertEqual(self.tap(origin_server_ts=1_700_000_000_500), "applied")
        body = json.loads((vendored.actions_dir(self.ws) / f"{self.req.id}.json").read_text())
        self.assertEqual(body["tapped_at"], 1_700_000_000)
        for ev in ({}, {"origin_server_ts": "x"}, {"origin_server_ts": False}, {"origin_server_ts": 0}, None):
            self.assertIsNone(vendored.event_time(ev))

    def test_a_tap_from_another_room_runs_nothing(self):
        self.assertEqual(self.tap(room="!elsewhere:s.space"), "rejected")
        self.assertFalse(vendored.actions_dir(self.ws).exists())

    def test_a_tap_on_an_old_revision_gets_a_stale_receipt_on_the_card(self):
        old = self.req.revision
        with self.mgr.store.locked():
            cur = self.mgr.get(self.req.id)
            cur.refresh_guard(cur.guard)
            self.mgr.store.save(cur)
        self.assertEqual(self.tap(revision=old), "rejected")
        card = self.mgr.get(self.req.id)
        self.assertEqual(card.subject["receipt"]["outcome"], "stale")
        self.assertEqual(card.to_wire()["subject"], {"receipt": {"outcome": "stale", "at": card.subject["receipt"]["at"]}})
        self.assertEqual(card.revision, old + 2, "a fresh revision past the one tapped")

    def test_a_card_closed_after_done_but_still_blocked_says_so(self):
        from ag2_sparrow.hitl import renotify
        from ag2_sparrow.hitl.projector import fallback_body
        with self.mgr.store.locked():
            cur = self.mgr.get(self.req.id)
            cur.subject = {**cur.subject, "receipt": {"outcome": "done", "at": 1}, "superseded": True}
            self.mgr.store.save(cur)
        self.mgr.resolve(self.req.id)
        card = self.mgr.get(self.req.id)
        self.assertTrue(fallback_body(card).endswith("— Still blocked."))
        self.assertEqual(card.to_wire()["subject"], {"receipt": {"outcome": "done", "at": 1}, "superseded": True})
        self.assertTrue(renotify.said_on_card(card))
        up = SimpleNamespace(subject={"recovery": True, "verify": {"seen": "up"}})
        blocked = SimpleNamespace(subject={"recovery": True, "verify": {"seen": "blocked"}})
        self.assertTrue(renotify.said_on_card(up))
        self.assertFalse(renotify.said_on_card(blocked), "still blocked alone owes the notice")

    def test_a_stale_stamp_never_breaks_the_rejection(self):
        from unittest import mock
        with mock.patch.object(self.mgr.store, "save", side_effect=OSError("disk")):
            self.handler._stamp_stale(self.req.id)  # logged, never raised



class VendoredAuditLock(unittest.TestCase):
    def test_a_bounded_audit_gives_up_on_a_held_lock_and_writes_once_it_is_free(self):
        import threading
        from ag2_sparrow.file_lock import locked_file
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertTrue(vendored.audit(ws, {"n": 1}, wait_s=1))
            path = vendored.recovery_dir(ws) / vendored.AUDIT_FILE
            held = []
            with locked_file(path.with_name(vendored.AUDIT_LOCK)):
                waiter = threading.Thread(target=lambda: held.append(vendored.audit(ws, {"n": 2}, wait_s=0.2)))
                waiter.start()
                waiter.join(10)
            self.assertEqual(held, [False], "a busy lock is a line not written, within the wait")
            self.assertTrue(vendored.audit(ws, {"n": 3}, wait_s=1))
            self.assertEqual([json.loads(line)["n"] for line in path.read_text().splitlines()], [1, 3])

if __name__ == "__main__":
    unittest.main()

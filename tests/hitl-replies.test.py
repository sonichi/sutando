"""Client action wire, inbound half: an owner's `space.ag2.hitl.action` message
is claimed, gated (owner only; revision + guard), applied (-> in_progress with
chosen_action), and — for TUI-sourced requirements only — handed to the runtime
driver as an action file. Unrelated traffic is never claimed; a rejected reply
changes nothing. Chain-compatible with the bridge's HandlerChain."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from hitl.events import events_dir, ingest  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.replies import REPLY_FIELD, HitlReplyHandler, actions_dir, parse_reply  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

OWNER = "@owner:ag2.space"


def event(payload, actor=OWNER, etype="message.created", eid="$e1", **content):
    c = {"body": "Allow", **content}
    if payload is not None:
        c[REPLY_FIELD] = payload
    return {"type": etype, "event_id": eid, "actor_id": actor, "content": c}


def reply_for(req, action_id, revision=None, guard=None):
    return {"hitl_id": req.id, "expected_revision": req.revision if revision is None else revision,
            "action_id": action_id, "guard": req.guard if guard is None else guard}


class ReplyHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        self.logs = []
        self.h = HitlReplyHandler(self.mgr, OWNER, workspace=self.ws, log=self.logs.append)
        self.req = self.mgr.create(HumanRequirement(
            kind="permission", runtime="claude", message="Claude wants to run Bash: rm -rf build",
            guard="hook:abc", device={"id": "core-1", "name": "core-1"},
            actions=[Action(id="allow", kind="allow_once", label="Allow"),
                     Action(id="deny", kind="reject_once", label="Deny"),
                     Action(id="open_terminal", kind="open_terminal", label="Open terminal")]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_reply_shapes(self):
        self.assertIsNone(parse_reply(event(None)))
        self.assertIsNone(parse_reply(event("not a dict")))
        self.assertIsNone(parse_reply(event({"hitl_id": "x"})))  # missing fields
        r = parse_reply(event(reply_for(self.req, "allow")))
        self.assertEqual((r.hitl_id, r.action_id, r.expected_revision, r.guard),
                         (self.req.id, "allow", self.req.revision, "hook:abc"))

    def test_claims_only_messages_carrying_the_field(self):
        self.assertTrue(self.h.claims(event(reply_for(self.req, "allow"))))
        self.assertFalse(self.h.claims(event(None)))
        self.assertFalse(self.h.claims(event(reply_for(self.req, "allow"), etype="reaction.added")))
        self.assertFalse(self.h.claims(event("junk")))

    def test_owner_reply_applies_and_unblocks(self):
        out = self.h.offer(event(reply_for(self.req, "allow")))
        self.assertEqual(out, ["$e1"])
        req = self.mgr.get(self.req.id)
        self.assertEqual((req.status, req.chosen_action), ("in_progress", "allow"))
        self.assertFalse(list(actions_dir(self.ws).glob("*.json")))  # a hook action needs no driver

    def test_non_owner_is_claimed_but_changes_nothing(self):
        out = self.h.offer(event(reply_for(self.req, "allow"), actor="@stranger:ag2.space"))
        self.assertEqual(out, ["$e1"])
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")
        self.assertTrue(any("non-owner" in l for l in self.logs))

    def test_no_owner_configured_is_inert(self):
        h = HitlReplyHandler(self.mgr, None, workspace=self.ws, log=self.logs.append)
        h.offer(event(reply_for(self.req, "allow")))
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")

    def test_stale_revision_and_wrong_guard_are_rejected(self):
        self.h.offer(event(reply_for(self.req, "allow", revision=self.req.revision + 5)))
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")
        self.h.offer(event(reply_for(self.req, "allow", guard="hook:other")))
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")
        self.assertEqual(sum("rejected" in l for l in self.logs), 2)

    def test_unknown_action_and_unknown_requirement_are_rejected(self):
        self.h.offer(event(reply_for(self.req, "nope")))
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")
        self.h.offer(event({"hitl_id": "hitl_missing", "expected_revision": 1, "action_id": "allow", "guard": ""}))
        self.assertEqual(sum("rejected" in l for l in self.logs), 2)

    def test_malformed_payload_is_claimed_and_ignored(self):
        out = self.h.offer(event({"hitl_id": self.req.id}))
        self.assertEqual(out, ["$e1"])
        self.assertEqual(self.mgr.get(self.req.id).status, "pending")

    def test_tui_reply_writes_the_driver_action_file(self):
        events_dir(self.ws).mkdir(parents=True)
        (events_dir(self.ws) / "core-2-g7.json").write_text(json.dumps({
            "schema": "space.ag2.hitl.runtime_event.v1", "session": "core-2", "socket": "/tmp/s.sock",
            "runtime": "claude", "kind": "permission", "prompt": "Do you want to proceed?", "guard": "g7",
            "observed_ms": 1, "options": [{"id": "1", "label": "Yes"}, {"id": "3", "label": "No"}]}))
        ingest(self.mgr, self.ws)
        tui = next(r for r in self.mgr.active() if r.guard == "g7")
        self.h.offer(event(reply_for(tui, "1"), eid="$e2"))
        self.assertEqual(self.mgr.get(tui.id).status, "in_progress")
        [path] = list(actions_dir(self.ws).glob("*.json"))
        self.assertEqual(path.name, f"{tui.id}.json")
        body = json.loads(path.read_text())
        self.assertEqual({k: body[k] for k in ("session", "socket", "guard", "action_id")},
                         {"session": "core-2", "socket": "/tmp/s.sock", "guard": "g7", "action_id": "1"})
        # The jump action is the client's, never the driver's.
        self.h.offer(event(reply_for(self.mgr.get(tui.id), "open_terminal"), eid="$e3"))
        self.assertEqual(len(list(actions_dir(self.ws).glob("*.json"))), 1)

    def test_no_workspace_means_no_driver_file_but_still_applies(self):
        h = HitlReplyHandler(self.mgr, OWNER, workspace=None, log=self.logs.append)
        h.offer(event(reply_for(self.req, "deny")))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")

    def test_in_the_bridge_chain_the_reply_never_reaches_taskify(self):
        from ag2_sparrow.human_action import HandlerChain

        seen = []

        class Default:
            last_path = None

            def offer(self, ev):
                seen.append(ev["event_id"])
                return [ev["event_id"]]

        chain = HandlerChain([self.h, Default()])
        self.assertEqual(chain.offer(event(reply_for(self.req, "allow"), eid="$click")), ["$click"])
        self.assertEqual(chain.offer(event(None, eid="$chat")), ["$chat"])
        self.assertEqual(seen, ["$chat"])
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "allow")


if __name__ == "__main__":
    unittest.main()

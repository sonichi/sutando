#!/usr/bin/env python3
"""remote-gateway-bridge: a card click on the TASK relay is applied to the HITL
store and consumed; ordinary messages and unconfigured hosts stay on the task path."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="hitl-relay-click-"))
for p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if p not in sys.path:
        sys.path.insert(0, p)
from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=TMP / "tasks", result_dir=TMP / "results", state_dir=TMP / "state")
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

OWNER = "@owner:ag2.space"


class TaskRelayClickTests(unittest.TestCase):
    def setUp(self):
        os.environ["SPARROW_HA_OWNER"] = OWNER
        self.mgr = HitlManager(HitlStore(default_store(TMP)))
        self.req = self.mgr.create(HumanRequirement(
            kind="permission", runtime="claude", message="Claude wants to run Bash: ls",
            guard=f"hook:{os.urandom(4).hex()}", device={"id": "worker-1", "name": "worker-1"},
            actions=[Action(id="allow", kind="allow_once", label="Allow"),
                     Action(id="deny", kind="reject_once", label="Deny")]))

    def _task(self, **kw):
        t = {"id": f"task-{os.urandom(5).hex()}", "channel_id": "!dm:ag2.space",
             "user_id": OWNER, "source_message_id": "$click", "task": "Allow"}
        t.update(kw)
        return t

    def test_non_owner_typed_label_reply_stays_a_message(self):
        """A non-owner's typed "Deny" is a message and must reach the task path;
        a non-owner's real click is still consumed. Own card id: the store
        outlives one test and requirement_for_event returns the first match."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-nonowner")
        t = self._task(user_id="@someone-else:ag2.space", reply_to_event="$card-nonowner", task="Deny")
        self.assertIs(rgb._handle_hitl_action(t), False)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        click = self._task(user_id="@someone-else:ag2.space",
                           hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                        "action_id": "deny", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(click), "ignored")
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_hitl_action_on_the_task_is_applied_and_consumed(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "allow")

    def test_reply_to_the_card_with_a_label_is_applied(self):
        self.mgr.record_projection(self.req.id, self.req.revision, "$card")
        self.assertTrue(rgb._handle_hitl_action(self._task(reply_to_event="$card", task="Deny")))
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")

    def test_ordinary_message_stays_on_the_task_path(self):
        self.assertFalse(rgb._handle_hitl_action(self._task(task="Allow")))
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_no_owner_configured_leaves_the_click_as_a_task(self):
        os.environ.pop("SPARROW_HA_OWNER", None)
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertFalse(rgb._handle_hitl_action(t))
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)

    def test_redelivered_click_is_consumed_without_a_second_apply(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # what the poll loop does after a consume
        rev = self.mgr.get(self.req.id).revision
        self.assertTrue(rgb._handle_hitl_action(t))
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)

    def test_redelivered_label_reply_is_consumed_while_its_control_result_waits(self):
        """The fallback form carries no hitl_action. While the [no-send] control
        result is still queued (results plane down), a redelivery must be held
        as consumed, not fall through to _write_task as the owner's chat."""
        self.mgr.record_projection(self.req.id, self.req.revision, "$card-redeliver")
        t = self._task(reply_to_event="$card-redeliver", task="Deny")
        self.assertTrue(rgb._handle_hitl_action(t))
        rgb._queue_review_control_result(t)  # POST /v1/results has not succeeded yet
        rev = self.mgr.get(self.req.id).revision
        out = rgb._handle_hitl_action(t)
        self.assertTrue(out, out)
        self.assertFalse(str(out).startswith("rejected:"), out)
        self.assertEqual(self.mgr.get(self.req.id).revision, rev)
        self.assertEqual(self.mgr.get(self.req.id).chosen_action, "deny")


    def test_stale_click_is_consumed_and_the_owner_is_told(self):
        stale = {"hitl_id": self.req.id, "expected_revision": self.req.revision + 5,
                 "action_id": "allow", "guard": self.req.guard}
        t = self._task(hitl_action=stale)
        out = rgb._handle_hitl_action(t)
        self.assertTrue(str(out).startswith("rejected:"), out)
        self.assertIsNone(self.mgr.get(self.req.id).chosen_action)
        body = "That click did not apply" if str(out).startswith("rejected:") else "[no-send]"
        rgb._queue_review_control_result(t, body=body)
        rec = json.loads(rgb._control_result_path(t["id"]).read_text())
        self.assertIn("did not apply", rec["body"])

    def test_applied_click_closes_silently(self):
        t = self._task(hitl_action={"hitl_id": self.req.id, "expected_revision": self.req.revision,
                                    "action_id": "allow", "guard": self.req.guard})
        self.assertEqual(rgb._handle_hitl_action(t), "applied")
        rgb._queue_review_control_result(t)
        self.assertEqual(json.loads(rgb._control_result_path(t["id"]).read_text())["body"], "[no-send]")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""connectors.py reauth: an app call refused because its sign-in lapsed gets a Signed out card.

- `auth_problem` tells connect_required (never connected) from a lapsed sign-in and from other errors.
- reauth arms the same wait as `card`, so the request resumes, and prints a room.message.send payload
  carrying an agent panel in state error (app logo, "Signed out · <account>", what it stopped, Reconnect).
- connect_required, a non-auth error and an app still listed active arm nothing; only the owner's own
  task may arm one.
- The card's Reconnect reply (skills/agent-cards cards.py reply) prints the Connect card of that wait.

Run: python3 tests/connect-apps-reauth.test.py
"""
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("connect_apps_cli_suite", ROOT / "tests" / "connect-apps-cli.test.py")
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)
connectors, ROOM, OWNER = S.connectors, S.ROOM, S.OWNER
sys.path.insert(0, str(ROOT / "src"))
import agent_card as ac  # noqa: E402
import issued_cards  # noqa: E402

GMAIL = {"kind": "connector", "slug": "gmail", "name": "Gmail", "authMode": "oauth"}
EXPIRED = "composio_exec failed: 401 Unauthorized: the connected account's token expired, re-authenticate"


def reauth_argv(error=EXPIRED, task="task-abc", **extra):
    argv = ["reauth", "gmail", "--room", ROOM, "--reply-to", "$evt1", "--task", task, "--owner-from-task",
            "--request", "any email from Sam", "--error", error]
    for k, v in extra.items():
        argv += [f"--{k}", v]
    return argv


class Classify(unittest.TestCase):
    def test_the_three_kinds(self):
        self.assertEqual(connectors.auth_problem("error: connect_required for gmail"), "not_connected")
        for text in (EXPIRED, "invalid_grant", "Token has been expired or revoked.", "HTTP 401",
                     "authentication failed", "please re-auth"):
            self.assertEqual(connectors.auth_problem(text), "signed_out", text)
        for text in ("rate limited", "404 not found", "invalid arguments: to", ""):
            self.assertIsNone(connectors.auth_problem(text), text)


class Reauth(S.Base):
    def cloud(self, rows=None):
        return S.FakeCloud(self.ws, items=[GMAIL], connections=rows or [])

    def run_(self, argv, cloud=None):
        return S.run(self.ws, argv, cloud or self.cloud(), spawn=self.spawn)

    def test_a_lapsed_sign_in_arms_a_wait_and_prints_the_signed_out_card(self):
        rows = [{"id": "c1", "toolkit": "gmail", "status": "EXPIRED", "accountLabel": "me@work.io"}]
        code, out = self.run_(reauth_argv(account="me@work.io", stopped="I couldn't check your inbox."),
                              self.cloud(rows))
        self.assertEqual(code, 0, out)
        self.assertTrue(out["wait_id"])
        self.assertEqual(self.spawned, [out["wait_id"]])
        msg = out["message"]
        self.assertEqual((msg["reply_to"], msg["operation_id"]), ("$evt1", "task-abc:reauth-card"))
        card = msg["extra_content"]["space.ag2.card"]
        self.assertIsNone(ac.problem(card))
        self.assertEqual((card["state"], card["header"]["title"]), ("error", "Gmail"))
        self.assertEqual(card["header"]["subtitle"], "Signed out · me@work.io")
        self.assertEqual(card["header"]["avatar"], {"app": "gmail", "shape": "square"})
        self.assertIn("I couldn't check your inbox.", card["text"])
        self.assertEqual(card["buttons"][0]["reply"], "card:apps:gmail:reconnect")
        self.assertNotIn("\n", msg["body"])
        marker = connectors.read_marker(self.ws, out["wait_id"])
        self.assertEqual((marker["request"], marker["owner"]), ("any email from Sam", OWNER))

    def test_connect_required_or_another_error_arms_nothing(self):
        for error, reason in (("connect_required", "use_card"), ("rate limited", "not_an_auth_error")):
            code, out = self.run_(reauth_argv(error=error))
            self.assertEqual((code, out["reason"], out["message"]), (1, reason, None))
        self.assertEqual((self.spawned, connectors.list_markers(self.ws)), ([], []))

    def test_an_app_still_listed_active_arms_nothing(self):
        code, out = self.run_(reauth_argv(), self.cloud([{"id": "c1", "toolkit": "gmail", "status": "ACTIVE"}]))
        self.assertEqual((code, out["reason"]), (1, "still_active"))
        self.assertEqual(connectors.list_markers(self.ws), [])

    def test_only_the_owner_s_own_task(self):
        self.origin(task="task-team", access_tier="team")
        code, out = self.run_(reauth_argv(task="task-team"))
        self.assertEqual((code, out["error"]), (2, "not_owner_task"))

    def test_the_reconnect_reply_brings_up_the_connect_card_of_that_wait(self):
        code, out = self.run_(reauth_argv())
        wait_id = out["wait_id"]
        recorded = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(out["message"]))), \
                contextlib.redirect_stdout(recorded):
            self.assertEqual(issued_cards.main(["record", "--workspace", str(self.ws), "--event-id", "$signed-out"]), 0)
        _spec2 = importlib.util.spec_from_file_location("cards", ROOT / "skills" / "agent-cards" / "scripts" / "cards.py")
        cards = importlib.util.module_from_spec(_spec2)
        _spec2.loader.exec_module(cards)
        task = self.ws / "tasks" / "task-click.txt"
        task.write_text("\n".join(["id: task-click", "task_layout: mid", "task: card:apps:gmail:reconnect",
                                   "source: ag2space", f"channel_id: {ROOM}", "channel_kind: dm",
                                   f"user_id: {OWNER}", "access_tier: owner", "reply_to_event: $signed-out"]) + "\n")
        printed = io.StringIO()
        with mock.patch.object(cards.connections, "owner_dm", lambda row, ws=None: ROOM), \
                mock.patch.object(cards, "connectors", connectors, create=True), \
                mock.patch.dict(sys.modules, {"connectors": connectors}), contextlib.redirect_stdout(printed):
            rc = cards.main(["--workspace", str(self.ws), "reply", "--task-file", str(task)],
                            core={"owner_mxid": OWNER, "instance": "x"})
        got = json.loads(printed.getvalue())
        self.assertEqual((rc, got["status"], got["wait_id"]), (0, "post", wait_id))
        self.assertEqual(got["message"]["extra_content"]["space.ag2.connector"]["toolkits"], [{"slug": "gmail"}])
        self.assertEqual(got["message"]["operation_id"], f"{wait_id}:reconnect-card")

    def test_an_explicit_owner_must_be_the_task_s_own(self):
        argv = [a for a in reauth_argv() if a != "--owner-from-task"] + ["--owner", "@someone:s.space"]
        code, out = self.run_(argv)
        self.assertEqual((code, out["error"]), (2, "not_owner_task"))
        argv = [a for a in reauth_argv() if a != "--owner-from-task"] + ["--owner", OWNER]
        code, out = self.run_(argv)
        self.assertEqual(code, 0, out)

    def test_signed_out_of_sutando_or_another_station_account_arms_nothing(self):
        code, out = self.run_(reauth_argv(), S.FakeCloud(self.ws, items=[GMAIL], token=None))
        self.assertEqual((code, out["error"]), (2, "not_signed_in"))
        stamp = {"version": 1, "has_station_entry": True, "cloud_user_id": "u-other", "spawned_at": "2026-09-15T00:00:00Z"}
        (self.ws / "state").mkdir(exist_ok=True)
        (self.ws / "state" / "station-core-stamp.json").write_text(json.dumps(stamp))
        code, out = self.run_(reauth_argv())
        self.assertEqual((code, out["reason"], out["message"]), (1, "account_changed", None))
        self.assertEqual(connectors.list_markers(self.ws), [])


if __name__ == "__main__":
    unittest.main()

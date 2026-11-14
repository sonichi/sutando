#!/usr/bin/env python3
"""connectors.py reauth: an app call refused because its sign-in lapsed gets a Signed out card.

- `auth_problem` tells connect_required (never connected) from a lapsed sign-in and from other errors.
- reauth arms the same wait as `card`, so the request resumes, and prints a room.message.send payload
  carrying an agent panel in state error (app logo, "Signed out · <account>", what it stopped, Reconnect).
- connect_required, a non-auth error and an app still listed active arm nothing; only the owner's own
  task may arm one, and only for the owner's checked DM with this agent (the card names their account).
- The card's Reconnect button is a `connect` action: the client signs the owner in to the app in place
  and sends nothing back, so the wait is armed (marker written, waiter started) before the card exists,
  for the same app the button names; the waiter then resumes the request once the app shows connected.

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

REAL_OWNER_DMS = connectors.owner_dms
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

    def setUp(self):
        super().setUp()
        p = mock.patch.object(connectors, "owner_dms", lambda ws, owner: {ROOM} if owner == OWNER else set())
        p.start()
        self.addCleanup(p.stop)

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
        self.assertEqual((card["header"]["subtitle"], card["header"]["status"]), ("me@work.io", {"label": "Signed out"}))
        self.assertEqual(card["header"]["avatar"], {"app": "gmail", "shape": "square"})
        self.assertIn("I couldn't check your inbox.", card["text"])
        self.assertEqual(card["buttons"][0], {"label": "Reconnect Gmail", "kind": "connect", "style": "primary",
                                              "toolkit": "gmail", "for": OWNER})
        self.assertEqual(issued_cards.offered(card), {}, "signing in happens in the client, not as a reply")
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

    def test_the_wait_is_armed_for_the_card_s_app_before_the_card_exists_and_resumes_the_request(self):
        order = []
        real = connectors.reauth_message

        def spawn(ws, wait_id):
            order.append(("waiter", wait_id, [k["slug"] for k in connectors.read_marker(ws, wait_id)["toolkits"]]))
            return 4242
        with mock.patch.object(connectors, "reauth_message",
                               lambda wait, *a, **k: order.append(("card", wait["wait_id"])) or real(wait, *a, **k)):
            code, out = S.run(self.ws, reauth_argv(), self.cloud(), spawn=spawn)
        wait_id = out["wait_id"]
        self.assertEqual(order, [("waiter", wait_id, ["gmail"]), ("card", wait_id)])
        button = out["message"]["extra_content"]["space.ag2.card"]["buttons"][0]
        self.assertEqual((button["kind"], button["toolkit"]), ("connect", "gmail"))
        connected = self.cloud([{"id": "c9", "toolkit": "gmail", "status": "ACTIVE"}])
        self.assertEqual(connectors.run_waiter(self.ws, wait_id, connected, sleep=lambda s: None), "connected")
        resumed = [p.read_text() for p in (self.ws / "tasks").glob("*.txt") if "any email from Sam" in p.read_text()
                   and p.name != "task-abc.txt"]
        self.assertEqual(len(resumed), 1, "the original request comes back as one resume task")

    def test_a_waiter_that_cannot_start_prints_no_card(self):
        def boom(ws, wait_id):
            raise OSError("no exec")
        with contextlib.redirect_stderr(io.StringIO()):
            code, out = S.run(self.ws, reauth_argv(), self.cloud(), spawn=boom)
        self.assertEqual((code, out["waiter_pid"]), (0, None))
        self.assertEqual([m["wait_id"] for m in connectors.list_markers(self.ws)], [out["wait_id"]],
                         "the wait stays on disk, so `rearm` or the apps card starts its waiter")

    def test_only_the_owner_s_checked_dm(self):
        argv = reauth_argv()
        argv[argv.index("--room") + 1] = "!team:s.space"
        code, out = self.run_(argv)
        self.assertEqual((code, out["error"]), (2, "not_owner_dm"))
        self.assertEqual((self.spawned, connectors.list_markers(self.ws)), ([], []))
        rows = [{"instance": "a", "owner_mxid": OWNER}, {"instance": "b", "owner_mxid": "@other:s.space"},
                {"instance": "w", "owner_mxid": OWNER, "worker": "w1"}, {"instance": "c", "owner_mxid": OWNER}]
        import connections
        with mock.patch.object(connections, "read_rows", lambda ws=None: rows), \
                mock.patch.object(connections, "owner_dm", lambda r, ws=None: {"a": ROOM, "c": ""}.get(r["instance"], "!x:s")):
            self.assertEqual(REAL_OWNER_DMS(self.ws, OWNER), {ROOM})

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

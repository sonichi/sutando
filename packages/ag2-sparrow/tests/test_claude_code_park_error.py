#!/usr/bin/env python3
"""claude-local's `inbox park --error-stdin`: a turn an API error ended parks with that error's scrubbed
line in place of the generic detail; with no usable error text, the detail. Run:
    python3 packages/ag2-sparrow/tests/test_claude_code_park_error.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import cli, ConnectBase, hold_consumer, inbox, unhold  # noqa: E402


# SDKAssistantMessageError, the StopFailure `error` values of the CLI version CI pins (2.1.303).
CLI_STOP_FAILURE_CODES = {"authentication_failed", "oauth_org_not_allowed", "account_on_hold", "verification_required",
                          "billing_error", "rate_limit", "overloaded", "invalid_request", "model_not_found",
                          "server_error", "unknown", "max_output_tokens", "cloud_credential_error"}


class ParkError(ConnectBase):
    def setUp(self):
        super().setUp()
        self.configure(ALLOW_NON_OWNER="0")
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        self.holder, self.consumer = hold_consumer(self.work)
        self.addCleanup(unhold, self.holder)
        self.root = str(self.work)

    def verb(self, *args, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin or b""))):
            rc = cli.main(["inbox", "--root", self.root, *args])
        return rc, json.loads(out.getvalue())

    def confirmed(self, task_id, headers="access_tier: owner\nchannel_kind: dm\n"):
        (self.inbox / (task_id + ".txt")).write_text("id: %s\n%stask: hi\n" % (task_id, headers))
        self.assertEqual(self.verb("claim", task_id)[0], 0)
        token = self.verb("authorize", task_id, "1", "--text-hash", "h")[1]["token"]
        self.verb("confirm", task_id, "turn-1", token, "--text-hash", "h")
        return task_id

    def test_park_of_an_api_error_turn_names_the_scrubbed_error_and_falls_back_to_the_detail(self):
        for t, error, want in (
                ("task-e1", b"Credit balance is too low\n", "ended without a reply (turn error: Credit balance is too low)"),
                ("task-e2", b"denied: /Users/someone/.claude/x\n", "(turn error: denied: <path>)"),
                ("task-e3", b"  \n", "ended without a reply (turn error without a reply)")):
            self.confirmed(t)
            rc, out = self.verb("park", t, "turn-1", "--detail", "turn error without a reply", "--error-stdin", stdin=error)
            self.assertEqual((rc, out["outcome"], out["parked"]), (0, "settled", "turn error without a reply"), t)
            body = (self.results / (t + ".txt")).read_text()
            self.assertIn(want, body, t)
            self.assertNotIn("/Users/someone", body)

    def test_a_known_cli_error_code_reads_as_its_plain_reason_and_an_unknown_one_is_shown_as_is(self):
        for t, error, want in (
                ("task-b1", b"billing_error\n", "(turn error: your Claude account is out of credits)"),
                ("task-b2", b"authentication_failed", "(turn error: Claude is signed out on this computer; sign in again)"),
                ("task-b3", b"rate_limit\n", "(turn error: Claude's usage limit was reached; try again later)"),
                ("task-b4", b"overloaded\n", "(turn error: Claude's service is busy; try again shortly)"),
                ("task-b5", b"model_not_found\n", "(turn error: model_not_found)"),
                ("task-b6", b"some_new_code\n", "(turn error: some_new_code)")):
            self.confirmed(t)
            rc, out = self.verb("park", t, "turn-1", "--detail", "turn error without a reply", "--error-stdin", stdin=error)
            self.assertEqual((rc, out["outcome"]), (0, "settled"), t)
            self.assertIn(want, (self.results / (t + ".txt")).read_text(), t)
        self.assertEqual(set(inbox.STOP_FAILURE_REASONS) - CLI_STOP_FAILURE_CODES, set())

    def test_without_the_flag_stdin_is_never_read_and_the_detail_stands(self):
        t = self.confirmed("task-d1")
        rc, out = self.verb("park", t, "turn-1", "--detail", "turn aborted without a reply", stdin=b"not an error\n")
        self.assertEqual((rc, out["outcome"]), (0, "settled"))
        body = (self.results / (t + ".txt")).read_text()
        self.assertIn("ended without a reply (turn aborted without a reply)", body)
        self.assertNotIn("not an error", body)

    def test_the_reason_reaches_only_the_owner_in_a_dm_and_anything_unknown_fails_closed(self):
        generic = "ended without a reply (turn error without a reply)"
        room = self.confirmed("task-r1", "access_tier: owner\nchannel_kind: room\n")
        no_kind = self.confirmed("task-k1", "access_tier: owner\n")
        collab = self.confirmed("task-c1", "access_tier: team\ncollaborator: true\nchannel_kind: dm\n")
        gone = self.confirmed("task-u1")
        (self.inbox / (gone + ".txt")).unlink()
        for t in (room, no_kind, collab, gone):
            rc, out = self.verb("park", t, "turn-1", "--detail", "turn error without a reply", "--error-stdin",
                                stdin=b"Credit balance is too low\n")
            self.assertEqual((rc, out["outcome"]), (0, "settled"), t)
            body = (self.results / (t + ".txt")).read_text()
            self.assertIn(generic, body, t)
            self.assertNotIn("Credit balance", body, t)
        for t in ("task-r2", "task-c2"):
            self.confirmed(t, "access_tier: owner\nchannel_kind: room\n" if t == "task-r2"
                           else "access_tier: team\ncollaborator: true\nchannel_kind: dm\n")
            self.verb("park", t, "turn-1", "--detail", "turn error without a reply", "--error-stdin", stdin=b"billing_error\n")
            body = (self.results / (t + ".txt")).read_text()
            self.assertIn(generic, body, t)
            self.assertNotIn("credits", body, t)
            self.assertNotIn("billing_error", body, t)
        self.assertEqual(inbox._audience(self.root, gone), (None, None))
        self.assertEqual(inbox._audience(self.root, collab), ("team", "dm"))
        self.assertEqual(inbox._audience(self.root, room), ("owner", "room"))
        self.assertEqual(inbox._audience(self.root, no_kind), ("owner", None))

    def test_park_called_directly_takes_the_error_argument(self):
        t = self.confirmed("task-p1")
        out = inbox.park(self.root, t, "turn-1", "no result", "You've hit your usage limit.\n  Try again later.")
        self.assertEqual(out["outcome"], "settled")
        self.assertIn("(turn error: You've hit your usage limit. Try again later.)",
                      (self.results / (t + ".txt")).read_text())


if __name__ == "__main__":
    unittest.main()

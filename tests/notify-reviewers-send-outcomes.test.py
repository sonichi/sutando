#!/usr/bin/env python3
"""The Discord sender's three outcomes, its mention proof, and its two owners.

A confirmed post reported as failed invites a retry the receipt calls UNSAFE,
and a mention array proves only that SOMEBODY was mentioned unless the target
is required by id.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from outbox import DeliveryOutcome, RetrySafety            # noqa: E402
from outbox_adapter import DeliveryReceipt                 # noqa: E402

SCRIPT = REPO / "skills" / "collaboration-intelligence" / "scripts" / "send_channel_message.py"


def load():
    spec = importlib.util.spec_from_file_location("scm", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeClient:
    def __init__(self, outcome, mentions):
        self.outcome, self.mentions, self.payloads = outcome, mentions, []

    def send_message_with_response(self, channel, payload):
        self.payloads.append(payload)
        return (DeliveryReceipt(outcome=self.outcome, receipt_id="m1",
                                safety=RetrySafety.UNSAFE, detail="detail"),
                200, {"id": "m1", "mentions": self.mentions})


def run_main(outcome, mentions, target="111"):
    m = load()
    client = FakeClient(outcome, mentions)
    m.send = lambda ch, body, uid, _c=client: (
        _c.send_message_with_response(ch, {"content": body,
                                           "allowed_mentions": {"parse": [], "users": [str(uid)]}})[0::2])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = m.main(["222", target, "hello"])
    return rc, out.getvalue(), err.getvalue(), client


class Outcomes(unittest.TestCase):
    def test_confirmed_with_the_target_mentioned_succeeds(self):
        rc, out, _, _ = run_main(DeliveryOutcome.CONFIRMED, [{"id": "111"}])
        self.assertEqual(rc, 0)
        self.assertIn("m1", out)

    def test_confirmed_is_never_reported_as_failed(self):
        # The defect this file exists for: `receipt.delivered` does not exist,
        # so every landed post read as a failure and invited an UNSAFE retry.
        rc, _, err, _ = run_main(DeliveryOutcome.CONFIRMED, [{"id": "111"}])
        self.assertEqual(rc, 0)
        self.assertNotIn("not delivered", err.lower())

    def test_not_delivered_is_a_plain_failure(self):
        rc, _, err, _ = run_main(DeliveryOutcome.NOT_DELIVERED, [{"id": "111"}])
        self.assertEqual(rc, 1)
        self.assertIn("NOT DELIVERED", err)

    def test_unknown_says_it_may_have_landed_and_warns_off_a_retry(self):
        rc, _, err, _ = run_main(DeliveryOutcome.OUTCOME_UNKNOWN, [{"id": "111"}])
        self.assertEqual(rc, 4)
        self.assertIn("MAY have landed", err)
        self.assertIn("Do not retry blindly", err)


class MentionProof(unittest.TestCase):
    def test_somebody_elses_mention_is_not_proof(self):
        # A non-empty array is satisfied by any user; the reviewer must be in it.
        rc, _, err, _ = run_main(DeliveryOutcome.CONFIRMED, [{"id": "999"}])
        self.assertEqual(rc, 3)
        self.assertIn("111", err)

    def test_empty_mentions_still_fails(self):
        self.assertEqual(run_main(DeliveryOutcome.CONFIRMED, [])[0], 3)

    def test_allowed_mentions_is_pinned_to_the_target(self):
        _, _, _, client = run_main(DeliveryOutcome.CONFIRMED, [{"id": "111"}])
        self.assertEqual(client.payloads[0]["allowed_mentions"],
                         {"parse": [], "users": ["111"]})


class Owners(unittest.TestCase):
    """The transport is not the only thing a production sender must go through."""

    def test_it_constructs_through_the_post_gate_factory(self):
        src = SCRIPT.read_text()
        self.assertIn("from channels.discord.post_gate import make_client", src)
        self.assertIn("make_client(", src)
        self.assertNotIn("DiscordRestClient(", src)

    def test_the_token_goes_through_the_shared_resolver(self):
        src = SCRIPT.read_text()
        self.assertIn("from channel_token import resolve_channel_token", src)
        self.assertIn("resolve_channel_token(", src)
        # The private parser this replaced read the .env file directly and
        # raised FileNotFoundError on an env-only host.
        self.assertNotIn("DISCORD_BOT_TOKEN=", src)



class TokenResolution(unittest.TestCase):
    """The shared resolver, exercised — not just asserted from the source text."""

    def test_env_only_host_resolves_without_an_env_file(self):
        m = load()
        import os
        prev = os.environ.get("DISCORD_BOT_TOKEN")
        os.environ["DISCORD_BOT_TOKEN"] = "env-token"
        os.environ["CLAUDE_CONFIG_DIR"] = "/nonexistent-config-dir"
        try:
            self.assertEqual(m.token(), "env-token")
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            if prev is None:
                os.environ.pop("DISCORD_BOT_TOKEN", None)
            else:
                os.environ["DISCORD_BOT_TOKEN"] = prev

    def test_no_token_anywhere_exits_rather_than_posting(self):
        # The resolver has THREE layers: env, .env file, vault. Clearing the
        # first two still resolves on a host whose vault holds the token.
        m = load()
        m.resolve_channel_token = lambda *a, **k: ""
        with self.assertRaises(SystemExit):
            m.token()


class Usage(unittest.TestCase):
    def test_wrong_arg_count_is_a_usage_error_not_a_send(self):
        m = load()
        sent = []
        m.send = lambda *a, **k: sent.append(a)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = m.main(["222", "only-two"])
        self.assertEqual(rc, 2)
        self.assertIn("usage:", err.getvalue())
        self.assertEqual(sent, [], "a malformed invocation must not reach the transport")

if __name__ == "__main__":
    unittest.main(verbosity=2)

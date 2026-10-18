#!/usr/bin/env python3
"""`_default_slack_sender` suppresses link/media unfurling on the owner alert DM.

Health output carries URLs, and this sender DMs the owner arbitrary alert text.
Slack expands up to 5 previews per message, so a multi-link alert arrives as a
wall of cards with the failure buried under them.

Raised by @sonichi reviewing #3632, which covered the bridge reply path and
task-progress but not this sender. Suppression happens AT THE SEND: stripping
URLs from the body would destroy the links the alert exists to deliver.
"""
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
sys.modules["hc"] = hc
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass


class OwnerAlertDmDoesNotUnfurl(unittest.TestCase):
    def _send(self, text="see https://example.com/a and https://example.com/b"):
        """Drive the real sender with the network stubbed; return the postMessage payload."""
        calls = []

        def fake_api(token, method, payload):
            calls.append((method, payload))
            if method == "conversations.open":
                return {"ok": True, "channel": {"id": "D123"}}
            return {"ok": True}

        with mock.patch.object(hc, "_slack_owner_creds", return_value=("xoxb-t", "U1")), \
             mock.patch.object(hc, "_slack_api", side_effect=fake_api):
            ok = hc._default_slack_sender(text)
        post = next(p for m, p in calls if m == "chat.postMessage")
        return ok, post

    def test_the_owner_alert_dm_suppresses_unfurling(self) -> None:
        ok, post = self._send()
        self.assertTrue(ok)
        self.assertIs(post.get("unfurl_links"), False, post)
        self.assertIs(post.get("unfurl_media"), False, post)

    def test_the_links_themselves_are_untouched(self) -> None:
        """Suppression is at the SEND. Stripping URLs would destroy the payload."""
        body = "see https://example.com/a and https://example.com/b"
        _, post = self._send(body)
        self.assertEqual(post["text"], body)

    def test_the_channel_still_comes_from_conversations_open(self) -> None:
        """CONTROL: the added keys must not disturb the existing contract."""
        _, post = self._send()
        self.assertEqual(post["channel"], "D123")


if __name__ == "__main__":
    unittest.main(verbosity=2)

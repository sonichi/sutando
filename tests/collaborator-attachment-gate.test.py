#!/usr/bin/env python3
"""Collaborator attachments: per-channel owner opt-in, verdict-only exemption.

The guard exempts ONLY the attach marker, ONLY when the adapter passes
allow_attach=True; redirects and the default-deny stay untouched, and a
withheld attach-only result is quarantined so an owner word can release it.
Path authorization is deliberately absent here — it stays with the transport
allowlist (one owner: src/policy/egress/attachment.py).
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, "src")
spec = importlib.util.spec_from_file_location("guard", "src/policy/egress/result.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
REPO = pathlib.Path(".")


class PolicyBehavior(unittest.TestCase):
    def setUp(self):
        self.sd = pathlib.Path(tempfile.mkdtemp(prefix="ca-"))

    def _guard(self, body, task_id, **kw):
        return guard.guard_result_for_tier(
            body, "team", REPO, suppress_journal=(self.sd, task_id), **kw)

    def test_default_deny_unchanged(self):
        body = "[file: /tmp/x.png]\nmedia"
        out, why = self._guard(body, "t1")
        self.assertNotEqual(out, body)
        self.assertIsNotNone(why)

    def test_allow_attach_passes_byte_identical(self):
        body = "[file: /tmp/x.png]\nmedia"
        out, why = self._guard(body, "t2", allow_attach=True)
        self.assertEqual(out, body)
        self.assertIsNone(why)

    def test_redirect_still_withheld_under_allow_attach(self):
        body = "[channel: 123]\nleak"
        out, _ = self._guard(body, "t3", allow_attach=True)
        self.assertNotEqual(out, body)

    def test_attach_only_withhold_is_quarantined_for_release(self):
        body = "[file: /tmp/x.png]\nmedia"
        self._guard(body, "t4")
        rec_path = guard.quarantined_attachment_path(self.sd, "t4")
        self.assertTrue(rec_path.is_file())
        rec = json.loads(rec_path.read_text())
        self.assertEqual(rec["withheld_body"], body)
        self.assertEqual(rec["status"], "withheld_attachment_pending")

    def test_redirect_bearing_body_is_never_quarantined(self):
        # a redirect is not releasable, so no record may invite releasing it
        self._guard("[channel: 99]\n[file: /tmp/x.png]\nboth", "t5")
        self.assertFalse(guard.quarantined_attachment_path(self.sd, "t5").is_file())

    def test_allowed_delivery_leaves_no_quarantine(self):
        self._guard("[file: /tmp/x.png]\nmedia", "t6", allow_attach=True)
        self.assertFalse(guard.quarantined_attachment_path(self.sd, "t6").is_file())


class BridgeWiring(unittest.TestCase):
    def test_channel_flag_resolver_default_deny(self):
        import importlib.machinery
        # source-level pin: the bridge computes allow_attach from collaborator
        # status AND the channel flag, and passes it to the guard
        src = open("src/discord-bridge.py").read()
        self.assertIn("channel_allows_collaborator_attachments(", src)
        self.assertIn("allow_attach=_allow_attach", src)
        # helper default-deny semantics, exercised directly
        ns = {}
        import re
        m = re.search(r"def channel_allows_collaborator_attachments.*?(?=\ndef )", src, re.S)
        exec(m.group(0), ns)
        fn = ns["channel_allows_collaborator_attachments"]
        self.assertFalse(fn({}, "123"))
        self.assertFalse(fn({"groups": {"123": {"collaboratorAttachments": False}}}, "123"))
        self.assertFalse(fn({"groups": {"123": True}}, "123"))          # bool cfg, no dict
        self.assertTrue(fn({"groups": {"123": {"collaboratorAttachments": True}}}, "123"))
        self.assertFalse(fn({"groups": {"123": {"collaboratorAttachments": "yes"}}}, "123"))  # is True only


if __name__ == "__main__":
    unittest.main(verbosity=1)

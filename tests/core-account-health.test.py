#!/usr/bin/env python3
"""The core's account health record (src/core_account_health.py), for Settings and the profile tab.

- state/core-account-health.json says ok | signed_out | quota, the runtime, the account when known and,
  for quota only, resets_at; it is rewritten only when that changes, so `since` is when the state began.
- The quota notice records quota on a limit and ok once it clears, when it was given a runtime (the
  Claude Code core's monitor); without one (the Codex seat, which records it itself) it writes nothing.

Run: python3 tests/core-account-health.test.py
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import core_account_health as h  # noqa: E402
import core_quota_notice  # noqa: E402


class Record(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_marks_and_keeps_since_while_the_state_holds(self):
        self.assertEqual(h.read(self.ws), {})
        self.assertTrue(h.mark(self.ws, "signed_out", "codex", "a@example.com", now=100))
        self.assertEqual(h.read(self.ws), {"state": "signed_out", "runtime": "codex",
                                           "account": "a@example.com", "since": 100})
        self.assertFalse(h.mark(self.ws, "signed_out", "codex", "a@example.com", now=200))
        self.assertTrue(h.mark(self.ws, "quota", "codex", "a@example.com", resets_at=900, now=300))
        self.assertEqual((h.read(self.ws)["since"], h.read(self.ws)["resets_at"]), (300, 900.0))
        self.assertTrue(h.mark(self.ws, "quota", "codex", "a@example.com", resets_at=950, now=400))
        self.assertEqual(h.read(self.ws)["since"], 300, "a later reset time is the same limit")

    def test_ok_drops_resets_at_and_keeps_the_account(self):
        h.mark(self.ws, "quota", "claude", "", resets_at=900, now=1)
        self.assertIsNone(h.read(self.ws)["account"])
        h.mark(self.ws, "quota", "codex", "a@example.com", resets_at=900, now=2)
        self.assertTrue(h.mark_ok(self.ws, "codex", now=3))
        self.assertEqual(h.read(self.ws), {"state": "ok", "runtime": "codex", "account": "a@example.com", "since": 3})
        self.assertFalse(h.mark_ok(self.ws, "codex", now=4))

    def test_only_known_states_and_runtimes(self):
        for bad in (("broken", "codex"), ("ok", "gemini")):
            with self.assertRaises(ValueError):
                h.mark(self.ws, *bad)

    def test_codex_email_reads_the_id_token_only(self):
        claims = base64.urlsafe_b64encode(json.dumps({"email": "a@example.com"}).encode()).decode().rstrip("=")
        f = self.ws / "auth.json"
        f.write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.sig"}}))
        self.assertEqual(h.codex_email(f), "a@example.com")
        f.write_text(json.dumps({"OPENAI_API_KEY": "sk-x"}))
        self.assertEqual(h.codex_email(f), "")
        self.assertEqual(h.codex_email(self.ws / "missing.json"), "")


class ViaQuotaNotice(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.now = [1000.0]

    def notice(self, **kw):
        return core_quota_notice.QuotaNotice(self.ws, "Claude Code", lambda text: True, log=lambda *a: None,
                                             now=lambda: self.now[0], **kw)

    def test_the_claude_monitor_records_quota_then_ok(self):
        q = self.notice(runtime="claude")
        q.cleared()
        self.assertEqual(h.read(self.ws), {}, "nothing to clear: no record")
        self.assertEqual(q.hit("3:05 PM", 5000), "quota-card")
        self.assertEqual({k: h.read(self.ws).get(k) for k in ("state", "runtime", "resets_at", "since")},
                         {"state": "quota", "runtime": "claude", "resets_at": 5000.0, "since": 1000.0})
        self.now[0] += 60
        q.hit("3:05 PM", 5000)
        self.assertEqual(h.read(self.ws)["since"], 1000.0)
        self.assertEqual(q.cleared(), "quota-cleared")
        self.assertEqual(h.read(self.ws)["state"], "ok")

    def test_without_a_runtime_it_writes_no_health(self):
        q = self.notice()
        q.hit("", None)
        q.cleared()
        self.assertEqual(h.read(self.ws), {})


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The room-ops READ path must scrub what the WRITE path scrubs.

`remote_gateway_bridge` filters a pasted secret before persisting a task file
(#2301). `read.py::_normalize` copied the message body straight through, so the
same secret came back out of the same room in the same process (#2945). These
pin the symmetry, not a particular regex — the reader delegates to
`src/chat_secret_filter`, so a pattern change lands on both sides at once.
"""
import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKILL = _ROOT / "skills" / "agent-room-ops"
if str(_SKILL) not in sys.path:
    sys.path.insert(0, str(_SKILL))


def _read_mod():
    spec = importlib.util.spec_from_file_location("room_ops_read", _SKILL / "read.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Fictitious, pattern-shaped only — never a real credential.
FAKE_TG = "1234567890:AAFAKEfakeFAKEfakeFAKEfakeFAKEfake123"


class ReadRedactsWhatWriteRedacts(unittest.TestCase):
    def test_a_pasted_secret_does_not_survive_the_read_path(self):
        m = _read_mod()
        out = m._normalize([{"sender": "@a:x", "event_id": "e1",
                             "body": f"vault set TELEGRAM_BOT_TOKEN {FAKE_TG}"}])
        self.assertNotIn(FAKE_TG, out[0]["body"],
                         "the reader handed back a secret the bridge would have scrubbed")

    def test_the_reader_matches_the_writer_exactly(self):
        """The point is PARITY. Comparing against the canonical filter (rather
        than asserting a fixed redacted string) is what keeps this true when the
        patterns change."""
        sys.path.insert(0, str(_ROOT / "src"))
        from chat_secret_filter import filter_chat_secrets
        raw = f"vault set TELEGRAM_BOT_TOKEN {FAKE_TG}"
        m = _read_mod()
        self.assertEqual(m._normalize([{"body": raw, "event_id": "e"}])[0]["body"],
                         filter_chat_secrets(raw).text)

    def test_ordinary_prose_is_untouched(self):
        """Without this, 'redact everything' would satisfy the tests above."""
        m = _read_mod()
        body = "the deploy finished, no issues"
        self.assertEqual(m._normalize([{"body": body, "event_id": "e"}])[0]["body"], body)

    def test_a_missing_body_stays_none_rather_than_becoming_a_string(self):
        m = _read_mod()
        self.assertIsNone(m._normalize([{"event_id": "e"}])[0]["body"])

    def test_other_fields_are_unaffected(self):
        m = _read_mod()
        out = m._normalize([{"sender": "@a:x", "ts": 7, "event_id": "e1",
                             "body": "hi", "reactions": [{"key": "eyes"}],
                             "media_ref": "mxc://x", "msgtype": "m.image"}])[0]
        self.assertEqual(out["sender"], "@a:x")
        self.assertEqual(out["ts"], 7)
        self.assertEqual(out["reactions"], [{"key": "eyes"}])
        self.assertEqual(out["media_ref"], "mxc://x")
        self.assertEqual(out["msgtype"], "m.image")

    def test_the_grammar_fallback_still_redacts_when_the_canonical_filter_is_gone(self):
        """The MIDDLE branch. Without this the fallback ladder has an untested
        rung, which is indistinguishable from a rung that never runs — and it is
        the one that carries the reported case (`vault set`)."""
        m = _read_mod()
        m._REDACTOR = None
        orig = sys.modules.pop("chat_secret_filter", None)
        try:
            sys.modules["chat_secret_filter"] = None      # force ImportError on the canonical one
            body = m._normalize([{"body": f"vault set TELEGRAM_BOT_TOKEN {FAKE_TG}",
                                  "event_id": "e"}])[0]["body"]
            self.assertNotIn(FAKE_TG, body)
            self.assertIn("VAULT-SET-REDACTED", body,
                          "expected the grammar fallback, not the fail-closed placeholder")
        finally:
            sys.modules.pop("chat_secret_filter", None)
            if orig is not None:
                sys.modules["chat_secret_filter"] = orig
            m._REDACTOR = None

    def test_redaction_failure_withholds_the_body_rather_than_leaking_it(self):
        """Fail CLOSED. Returning the raw body when no filter loads would be the
        exact defect this fixes, so the unavailable path must not degrade to it."""
        m = _read_mod()
        m._REDACTOR = None
        orig = sys.modules.pop("chat_secret_filter", None)
        orig_g = sys.modules.pop("vault_set_grammar", None)
        try:
            sys.modules["chat_secret_filter"] = None   # force ImportError
            sys.modules["vault_set_grammar"] = None
            body = m._normalize([{"body": f"vault set X {FAKE_TG}", "event_id": "e"}])[0]["body"]
            self.assertNotIn(FAKE_TG, body)
        finally:
            for k, v in (("chat_secret_filter", orig), ("vault_set_grammar", orig_g)):
                sys.modules.pop(k, None)
                if v is not None:
                    sys.modules[k] = v
            m._REDACTOR = None


class DegradedRedactorAnnouncesItself(unittest.TestCase):
    """A withheld body and a genuinely secret-laden one look identical downstream,
    and the choice is cached for the life of the process. Without a line at
    selection an operator cannot tell a broken install from a quiet room
    (reviewer's question on #2955)."""

    def _select_with(self, missing):
        m = _read_mod()
        m._REDACTOR = None
        saved = {k: sys.modules.pop(k, None) for k in missing}
        err = io.StringIO()
        try:
            for k in missing:
                sys.modules[k] = None          # force ImportError
            with contextlib.redirect_stderr(err):
                m._normalize([{"body": "hello", "event_id": "e"}])
        finally:
            for k in missing:
                sys.modules.pop(k, None)
                if saved.get(k) is not None:
                    sys.modules[k] = saved[k]
            m._REDACTOR = None
        return err.getvalue()

    def test_the_grammar_fallback_says_so(self):
        out = self._select_with(["chat_secret_filter"])
        self.assertIn("vault_set_grammar", out)
        self.assertIn("NOT filtered", out, "must say what it stopped covering")

    def test_the_withhold_state_says_it_is_pinned(self):
        out = self._select_with(["chat_secret_filter", "vault_set_grammar"])
        self.assertIn("WITHHOLDING", out)
        self.assertIn("pinned", out, "the operator needs to know a repair will not take effect")

    def test_the_healthy_path_is_silent(self):
        """Without this, logging unconditionally would satisfy both tests above."""
        m = _read_mod()
        m._REDACTOR = None
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            m._normalize([{"body": "hello", "event_id": "e"}])
        self.assertEqual(err.getvalue(), "", "the working redactor must not chatter")


if __name__ == "__main__":
    unittest.main(verbosity=2)

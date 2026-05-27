"""Tests for outbox_log integration in email-sender.py and whatsapp/scripts/send.py.

Phase B-1 of issue #932: skill-side senders write to outbox log.
Run: python3 tests/email-whatsapp-outbox.test.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_module(name: str, rel_path: str):
    """Load a module from a relative path, resetting its _outbox_log cache."""
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Don't cache under the same key across tests — fresh module per test.
    spec.loader.exec_module(mod)
    mod._outbox_log = None
    return mod


class TestEmailSenderOutboxLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    def _outbox_entries(self) -> list[dict]:
        path = self.workspace / "state" / "outbox.log"
        if not path.is_file():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def test_send_writes_outbox_entry(self):
        mod = _load_module("email_sender_test", "skills/macos-tools/scripts/email-sender.py")
        fake = MagicMock()
        fake.returncode = 0
        with patch.object(mod.subprocess, "run", return_value=fake):
            mod.send("bob@example.com", "Hello", "Body text", draft=False)

        entries = self._outbox_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["channel_type"], "email")
        self.assertEqual(e["recipient"], "bob@example.com")
        self.assertEqual(e["body_preview"], "Body text")
        self.assertEqual(e["body_len"], len("Body text"))

    def test_draft_does_not_write_outbox(self):
        mod = _load_module("email_sender_draft_test", "skills/macos-tools/scripts/email-sender.py")
        fake = MagicMock()
        fake.returncode = 0
        with patch.object(mod.subprocess, "run", return_value=fake):
            mod.send("bob@example.com", "Hello", "Draft body", draft=True)

        self.assertEqual(self._outbox_entries(), [])

    def test_failed_send_does_not_write_outbox(self):
        mod = _load_module("email_sender_fail_test", "skills/macos-tools/scripts/email-sender.py")
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "osascript: error"
        with patch.object(mod.subprocess, "run", return_value=fake):
            with self.assertRaises(SystemExit):
                mod.send("bob@example.com", "Hello", "Body", draft=False)

        self.assertEqual(self._outbox_entries(), [])


class TestWhatsAppSendOutboxLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    def _outbox_entries(self) -> list[dict]:
        path = self.workspace / "state" / "outbox.log"
        if not path.is_file():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def test_successful_send_writes_outbox_entry(self):
        mod = _load_module("wa_send_test", "skills/whatsapp/scripts/send.py")
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = "Message sent.\n"
        with patch.object(mod.subprocess, "run", return_value=fake):
            mod.send("+14155551234", "Hello WhatsApp!")

        entries = self._outbox_entries()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["channel_type"], "whatsapp")
        self.assertEqual(e["recipient"], "+14155551234")
        self.assertEqual(e["body_preview"], "Hello WhatsApp!")
        self.assertEqual(e["body_len"], len("Hello WhatsApp!"))

    def test_failed_send_does_not_write_outbox(self):
        mod = _load_module("wa_send_fail_test", "skills/whatsapp/scripts/send.py")
        fake = MagicMock()
        fake.returncode = 1
        fake.stderr = "wacli: not authenticated"
        fake.stdout = ""
        with patch.object(mod.subprocess, "run", return_value=fake):
            with self.assertRaises(SystemExit):
                mod.send("+14155551234", "Should not log")

        self.assertEqual(self._outbox_entries(), [])

    def test_group_jid_recipient_recorded(self):
        mod = _load_module("wa_group_test", "skills/whatsapp/scripts/send.py")
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = ""
        with patch.object(mod.subprocess, "run", return_value=fake):
            mod.send("1234567890@g.us", "Group message")

        entries = self._outbox_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["recipient"], "1234567890@g.us")


if __name__ == "__main__":
    unittest.main()

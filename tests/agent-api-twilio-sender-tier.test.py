#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="agent-api-twilio-tier-")
os.environ.setdefault("SUTANDO_TELEMETRY", "0")
os.environ["SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER"] = "1"
os.environ.pop("SUTANDO_API_TOKEN", None)
os.environ.pop("OWNER_NUMBER", None)

spec = importlib.util.spec_from_file_location("agent_api_twilio_tier", REPO / "src" / "agent-api.py")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
assert spec.loader is not None
spec.loader.exec_module(api)
api._emit_task_processed = lambda _content: None


class TwilioSenderTierTests(unittest.TestCase):
    def run_handler(self, method, sender, owner_numbers):
        task_dir = Path(tempfile.mkdtemp(prefix="agent-api-twilio-task-"))
        api.TASK_DIR = task_dir
        if owner_numbers is None:
            os.environ.pop("OWNER_NUMBER", None)
        else:
            os.environ["OWNER_NUMBER"] = owner_numbers
        handler = api.Handler.__new__(api.Handler)
        handler.send_twiml = Mock()
        handler.send_json = Mock()
        form_data = {"From": [sender], "Body": ["hello"], "TranscriptionText": ["hello"], "CallSid": ["CA1"]}
        getattr(handler, method)(form_data)
        files = list(task_dir.glob("task-*.txt"))
        self.assertEqual(len(files), 1)
        return files[0].read_text()

    def test_each_inbound_handler_assigns_sender_tier_from_current_owner_number(self):
        for method in ("handle_twilio_voice", "handle_twilio_sms", "handle_twilio_transcription"):
            with self.subTest(method=method, case="non-owner"):
                self.assertIn("access_tier: other", self.run_handler(method, "+14155550000", "+14155551234"))
            with self.subTest(method=method, case="formatted-owner"):
                self.assertIn("access_tier: owner", self.run_handler(method, "(415) 555-1234", "+14155551234"))
            with self.subTest(method=method, case="unset-owner"):
                self.assertIn("access_tier: other", self.run_handler(method, "+14155550000", None))

    def test_transcription_without_from_resolves_caller_from_voice_record(self):
        task_dir = Path(tempfile.mkdtemp(prefix="agent-api-twilio-task-"))
        api.TASK_DIR = task_dir
        os.environ["OWNER_NUMBER"] = "+14155551234"
        handler = api.Handler.__new__(api.Handler)
        handler.send_twiml = Mock()
        handler.send_json = Mock()
        handler.handle_twilio_voice(
            {"From": ["+14155551234"], "CallSid": ["CA-owner"]})
        time.sleep(0.002)
        handler.handle_twilio_transcription(
            {"TranscriptionText": ["hello"], "CallSid": ["CA-owner"]})
        time.sleep(0.002)
        handler.handle_twilio_transcription(
            {"TranscriptionText": ["hi"], "CallSid": ["CA-stranger"]})
        voicemails = [
            f.read_text()
            for f in sorted(task_dir.glob("task-*.txt"))
            if "twilio_voicemail" in f.read_text()
        ]
        self.assertEqual(len(voicemails), 2)
        self.assertIn("access_tier: owner", voicemails[0])
        self.assertIn("access_tier: other", voicemails[1])

    def test_expired_caller_record_is_pruned_from_disk_on_read(self):
        path = api.WORKSPACE_DIR / "state" / "twilio-callers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"CA-old": {"caller": "+14155550000", "expires_at": 1}}')
        with api._TWILIO_CALLERS_LOCK:
            self.assertEqual(api._twilio_caller_records(time.time()), {})
        self.assertNotIn("+14155550000", path.read_text())

    def test_failed_prune_still_returns_the_live_caller(self):
        path = api.WORKSPACE_DIR / "state" / "twilio-callers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"CA-old": {"caller": "+14155550000", "expires_at": 1},'
            ' "CA-new": {"caller": "+14155551234", "expires_at": 9999999999}}')
        with api._TWILIO_CALLERS_LOCK:
            with unittest.mock.patch.object(
                    api, "_write_twilio_callers", side_effect=OSError("read-only")):
                records = api._twilio_caller_records(time.time())
        self.assertEqual(records["CA-new"]["caller"], "+14155551234")

    def test_caller_record_file_that_is_not_a_dict_reads_empty(self):
        path = api.WORKSPACE_DIR / "state" / "twilio-callers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["CA-1"]')
        with api._TWILIO_CALLERS_LOCK:
            self.assertEqual(api._twilio_caller_records(time.time()), {})
        path.write_text('not json')
        with api._TWILIO_CALLERS_LOCK:
            self.assertEqual(api._twilio_caller_records(time.time()), {})
        path.unlink()
        with api._TWILIO_CALLERS_LOCK:
            self.assertEqual(api._twilio_caller_records(time.time()), {})

    def test_remember_caller_ignores_values_outside_the_record_contract(self):
        path = api.WORKSPACE_DIR / "state" / "twilio-callers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        with api._TWILIO_CALLERS_LOCK:
            api._remember_twilio_caller("", "+14155550000")
            api._remember_twilio_caller("CA-1", "unknown")
            api._remember_twilio_caller("CA-1", "+" + "1" * 65)
        self.assertEqual(api._twilio_caller_records(time.time()), {})

    def test_caller_lookup_rejects_an_oversized_sid_before_reading(self):
        self.assertEqual(api._twilio_caller_for_call(""), "")
        self.assertEqual(api._twilio_caller_for_call("C" * 129), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

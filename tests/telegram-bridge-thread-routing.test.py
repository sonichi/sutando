#!/usr/bin/env python3
"""Coverage for message_thread_id routing added in PR #1889 (telegram-bridge.py).

Exercises send_file(), send_reply(), and poll_progress() with an explicit
message_thread_id so the forum-topic-routing branches (added so replies land
in the originating topic instead of the forum's General topic) are covered,
not just exercised implicitly via the reply-parsed-body structural test.

Run: python3 tests/telegram-bridge-thread-routing.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tgbridge_thread_routing", ROOT / "src" / "telegram-bridge.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


class SendFileThreadIdTest(unittest.TestCase):
    def test_message_thread_id_included_in_multipart_body(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.write(b"hello")
        tmp.close()
        try:
            captured = {}

            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"ok": true}'

            def fake_urlopen(req, timeout=30):
                captured["body"] = req.data
                return FakeResp()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = tg.send_file(555, tmp.name, message_thread_id=42)
            self.assertTrue(result.get("ok"))
            self.assertIn(b'name="message_thread_id"', captured["body"])
            self.assertIn(b"\r\n42\r\n", captured["body"])
        finally:
            os.unlink(tmp.name)

    def test_no_message_thread_id_when_unset(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.write(b"hello")
        tmp.close()
        try:
            captured = {}

            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"ok": true}'

            def fake_urlopen(req, timeout=30):
                captured["body"] = req.data
                return FakeResp()

            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                tg.send_file(555, tmp.name)
            self.assertNotIn(b'name="message_thread_id"', captured["body"])
        finally:
            os.unlink(tmp.name)


class SendReplyThreadIdTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_api(method, **params):
            self.calls.append((method, params))
            return {"ok": True}

        self._orig_api = tg.api
        tg.api = fake_api

    def tearDown(self):
        tg.api = self._orig_api

    def test_text_reply_carries_message_thread_id(self):
        tg.send_reply(555, "hello there", message_thread_id=42)
        sends = [p for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].get("message_thread_id"), 42)

    def test_text_reply_omits_message_thread_id_when_unset(self):
        tg.send_reply(555, "hello there")
        sends = [p for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("message_thread_id", sends[0])

    def test_file_marker_forwards_message_thread_id_to_send_file(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.write(b"data")
        tmp.close()
        try:
            captured = {}

            def fake_send_file(chat_id, fpath, caption="", message_thread_id=None):
                captured["message_thread_id"] = message_thread_id
                return {"ok": True}

            with patch.object(tg, "send_file", side_effect=fake_send_file), \
                 patch.object(tg, "_is_path_sendable", return_value=True):
                result = tg.send_reply(555, f"see attached [file: {tmp.name}]", message_thread_id=42)
            self.assertEqual(captured.get("message_thread_id"), 42)
            self.assertEqual(result["files_sent"], 1)
        finally:
            os.unlink(tmp.name)

    def test_blocked_file_notice_carries_message_thread_id(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        tmp.write(b"data")
        tmp.close()
        try:
            # Real file that exists but is NOT under an allowed root/prefix —
            # triggers the "BLOCKED file" branch (os.path.isfile True,
            # _is_path_sendable False). No other text in the body, so this is
            # the only sendMessage call (clean_text is empty after marker
            # extraction).
            with patch.object(tg, "_is_path_sendable", return_value=False):
                tg.send_reply(555, f"[file: {tmp.name}]", message_thread_id=42)
            sends = [p for m, p in self.calls if m == "sendMessage"]
            self.assertEqual(len(sends), 1)
            self.assertIn("access denied", sends[0]["text"])
            self.assertEqual(sends[0].get("message_thread_id"), 42)
        finally:
            os.unlink(tmp.name)


class PollProgressThreadIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = Path(self.tmp) / "state"
        self.results = Path(self.tmp) / "results"
        self.state.mkdir()
        self.results.mkdir()
        tg.STATE_DIR = self.state
        tg.RESULTS_DIR = self.results
        tg._progress_msgs.clear()
        tg.pending_task_tiers.clear()
        self.calls = []
        self._mid = 1000

        def fake_api(method, **params):
            self.calls.append((method, params))
            if method == "sendMessage":
                self._mid += 1
                return {"ok": True, "result": {"message_id": self._mid}}
            return {"ok": True}

        self._orig_api = tg.api
        tg.api = fake_api
        os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
        (self.state / "core-status.json").write_text(
            json.dumps({"status": "running", "step": "Researching flights", "ts": int(time.time())})
        )

    def tearDown(self):
        tg.api = self._orig_api

    def _task_id(self, age_s):
        return f"task-{int((time.time() - age_s) * 1000)}"

    def test_placeholder_creation_carries_message_thread_id(self):
        tid = self._task_id(age_s=12)  # > threshold
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: (555, 42)})
        sends = [p for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0].get("message_thread_id"), 42)

    def test_placeholder_creation_omits_message_thread_id_for_bare_chat_id(self):
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})  # legacy bare-chat_id form, still supported
        sends = [p for m, p in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("message_thread_id", sends[0])


if __name__ == "__main__":
    unittest.main()

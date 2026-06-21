#!/usr/bin/env python3
"""Contract tests for telegram-bridge progress streaming (Telegram parity, feat/telegram-progress-parity).

Drives src/telegram-bridge.py:poll_progress through its lifecycle with a mocked
Telegram `api()` and a temp workspace, asserting the owner-only / threshold /
edit-rate / delete-on-done / flag-off behaviors match the Discord parity contract.

Run: python3 tests/telegram-bridge-progress-stream.test.py
"""
from __future__ import annotations
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")  # avoid the not-set warning path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tgbridge", ROOT / "src" / "telegram-bridge.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


class PollProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = Path(self.tmp) / "state"
        self.results = Path(self.tmp) / "results"
        self.state.mkdir()
        self.results.mkdir()
        # Redirect module dirs to temp
        tg.STATE_DIR = self.state
        tg.RESULTS_DIR = self.results
        # Reset module progress state
        tg._progress_msgs.clear()
        tg.pending_task_tiers.clear()
        # Capture api() calls; sendMessage returns a fake message_id
        self.calls = []
        self._mid = 1000

        def fake_api(method, **params):
            self.calls.append((method, params))
            if method == "sendMessage":
                self._mid += 1
                return {"ok": True, "result": {"message_id": self._mid}}
            return {"ok": True}

        tg.api = fake_api
        # Enable streaming
        os.environ["SUTANDO_PROGRESS_STREAM"] = "1"
        # Core is actively working
        self._write_status("running", "Researching flights")

    def _write_status(self, status, step):
        (self.state / "core-status.json").write_text(
            json.dumps({"status": status, "step": step, "ts": int(time.time())})
        )

    def _task_id(self, age_s):
        # task-<ms>; created `age_s` seconds ago
        return f"task-{int((time.time() - age_s) * 1000)}"

    def _methods(self):
        return [m for m, _ in self.calls]

    def test_no_placeholder_before_threshold(self):
        tid = self._task_id(age_s=3)  # < 8s
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        self.assertNotIn("sendMessage", self._methods())

    def test_placeholder_after_threshold_owner(self):
        tid = self._task_id(age_s=12)  # > 8s
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        self.assertIn("sendMessage", self._methods())
        self.assertIn(tid, tg._progress_msgs)
        # The placeholder text reflects the live step
        send = next(p for m, p in self.calls if m == "sendMessage")
        self.assertIn("Researching flights", send["text"])

    def test_no_placeholder_non_owner(self):
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "team"  # non-owner
        tg.poll_progress({tid: 555})
        self.assertNotIn("sendMessage", self._methods())

    def test_fail_closed_unknown_tier(self):
        tid = self._task_id(age_s=12)
        # tier NOT recorded (e.g. post-restart recovery)
        tg.poll_progress({tid: 555})
        self.assertNotIn("sendMessage", self._methods())

    def test_delete_on_done(self):
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})  # posts placeholder
        self.assertIn(tid, tg._progress_msgs)
        # Result lands
        (self.results / f"{tid}.txt").write_text("done!")
        tg.poll_progress({tid: 555})
        self.assertIn("deleteMessage", self._methods())
        self.assertNotIn(tid, tg._progress_msgs)
        self.assertNotIn(tid, tg.pending_task_tiers)

    def test_send_failure_marks_terminal_no_retry_spam(self):
        # If sendMessage fails (no message_id), the task is marked terminal so the
        # next ticks don't re-hammer the API every second.
        def failing_api(method, **params):
            self.calls.append((method, params))
            return {"ok": False}  # no result.message_id
        tg.api = failing_api
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        self.assertEqual(tg._progress_msgs.get(tid), {"expired": True})
        n1 = sum(1 for m, _ in self.calls if m == "sendMessage")
        tg.poll_progress({tid: 555})  # second tick must NOT send again
        n2 = sum(1 for m, _ in self.calls if m == "sendMessage")
        self.assertEqual(n1, n2)

    def test_fast_task_tier_gc_no_leak(self):
        # A task that got a tier but never a placeholder (finished fast) must not
        # leak in pending_task_tiers once it leaves pending_replies.
        tid = self._task_id(age_s=2)  # below threshold → no placeholder
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        self.assertNotIn(tid, tg._progress_msgs)
        tg.poll_progress({})  # task gone from pending_replies → GC must drop the tier
        self.assertNotIn(tid, tg.pending_task_tiers)

    def test_flag_off_is_noop(self):
        os.environ["SUTANDO_PROGRESS_STREAM"] = "0"
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        self.assertEqual(self.calls, [])

    def test_clear_progress_cleans_tier_when_off(self):
        # Even with the feature off, _clear_progress must drop the tier (no leak).
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg._clear_progress(tid)
        self.assertNotIn(tid, tg.pending_task_tiers)

    def test_idle_core_yields_generic_label(self):
        # If the core is idle (no step), placeholder still posts a generic "working…"
        self._write_status("idle", "")
        tid = self._task_id(age_s=12)
        tg.pending_task_tiers[tid] = "owner"
        tg.poll_progress({tid: 555})
        send = next((p for m, p in self.calls if m == "sendMessage"), None)
        self.assertIsNotNone(send)
        self.assertIn("working", send["text"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)

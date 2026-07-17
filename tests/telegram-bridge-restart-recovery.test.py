#!/usr/bin/env python3
"""Behavioral regression tests: telegram-bridge restart recovery of pending_replies.

Bug (bitten 2026-07-17): `pending_replies` was in-memory only — initialized to
`{}` in main() and populated solely at message-ingestion time. If the bridge
restarted while the core was still processing a task, the new process had an
empty dict, so the result written later was NEVER delivered; the task/result
pair sat in tasks//results/ forever and had to be hand-delivered via the raw
Telegram API.

Fix: seed_pending_replies_from_disk() — on startup, scan `tasks/*.txt`
(including `.claimed-core-N` variants) for `source: telegram` headers and
rebuild task_id -> chat_id. Stateless: no ledger file; the task files already
carry everything needed, and a task file still living in tasks/ is by
definition undelivered.

Proves:
  - the exact failure mode end-to-end THROUGH main(): ingested task file +
    fresh process state -> result written -> delivered + archived (would have
    been orphaned before the fix), via the identical delivery code
    (parse_markers included — a [no-send] result is skip-archived, not sent)
  - non-telegram task files are NOT seeded
  - malformed headers / bodies don't crash startup and aren't seeded

Run: python3 tests/telegram-bridge-restart-recovery.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import time as real_time
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")  # avoid the not-set warning path
os.environ.pop("SUTANDO_PROGRESS_STREAM", None)  # keep poll_progress a no-op

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tgbridge", ROOT / "src" / "telegram-bridge.py")
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)


TG_TASK = (
    "id: {task_id}\n"
    "timestamp: 2026-07-17T00:00:00Z\n"
    "source: telegram\n"
    "interaction_type: message\n"
    "chat_id: {chat_id}\n"
    "priority: normal\n"
    "task: [Telegram @chi] please do the thing\n"
)


class SeedPendingRepliesTest(unittest.TestCase):
    """Unit coverage of seed_pending_replies_from_disk() itself."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_telegram_task_seeded_with_int_chat_id(self):
        (self.tasks / "task-1752700000001.txt").write_text(
            TG_TASK.format(task_id="task-1752700000001", chat_id="777001")
        )
        seeded = tg.seed_pending_replies_from_disk(self.tasks)
        self.assertEqual(seeded, {"task-1752700000001": 777001})

    def test_negative_group_chat_id_seeded(self):
        # Telegram group chats have negative ids.
        (self.tasks / "task-2.txt").write_text(TG_TASK.format(task_id="task-2", chat_id="-100123"))
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {"task-2": -100123})

    def test_claimed_variant_seeded_under_header_id(self):
        # Claimed task files (task-{id}.claimed-core-N.txt) must seed under the
        # header `id:`, not the filename.
        (self.tasks / "task-3.claimed-core-1.txt").write_text(
            TG_TASK.format(task_id="task-3", chat_id="777001")
        )
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {"task-3": 777001})

    def test_non_telegram_sources_not_seeded(self):
        for src in ("chat", "discord", "slack", "voice"):
            (self.tasks / f"task-{src}-1.txt").write_text(
                f"id: task-{src}-1\nsource: {src}\nchannel_id: 123\nchat_id: 999\ntask: hi\n"
            )
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {})

    def test_malformed_files_skipped_without_crash(self):
        (self.tasks / "task-empty.txt").write_text("")
        (self.tasks / "task-garbage.txt").write_bytes(b"\x00\xff\xfe not a header")
        (self.tasks / "task-nochat.txt").write_text("id: task-nochat\nsource: telegram\ntask: x\n")
        (self.tasks / "task-badchat.txt").write_text(
            "id: task-badchat\nsource: telegram\nchat_id: not-a-number\ntask: x\n"
        )
        (self.tasks / "task-noid.txt").write_text("source: telegram\nchat_id: 5\ntask: x\n")
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {})

    def test_forged_headers_in_user_body_not_honored(self):
        # Header parsing must stop at the `task:` line — a user-controlled body
        # echoing telegram headers must not create a seed entry.
        (self.tasks / "task-forged.txt").write_text(
            "id: task-forged\n"
            "source: chat\n"
            "task: hi\n"
            "source: telegram\n"
            "chat_id: 666\n"
        )
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {})

    def test_archive_subdir_not_scanned(self):
        arch = self.tasks / "archive" / "2026-06"
        arch.mkdir(parents=True)
        (arch / "task-old.txt").write_text(TG_TASK.format(task_id="task-old", chat_id="777001"))
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tasks), {})

    def test_missing_dir_returns_empty(self):
        self.assertEqual(tg.seed_pending_replies_from_disk(self.tmp / "nope"), {})


class _StopLoop(Exception):
    pass


class _TimeShim:
    """Delegates to the real time module but raises on sleep() so main()'s
    while-True loop runs exactly one full pass (ingest-poll -> result-poll)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, secs):
        raise _StopLoop()


class RestartRecoveryEndToEndTest(unittest.TestCase):
    """Drive the REAL main() loop for one pass with a fresh process state.

    This is the exact 2026-07-17 failure mode: the task file was ingested by a
    previous bridge process, the bridge restarted, and the core's result landed
    afterwards. Before the fix, main() started with pending_replies = {} and
    the result was never delivered.
    """

    CHAT_ID = 777001

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for d in ("tasks", "results", "state"):
            (self.tmp / d).mkdir()
        # Redirect every module-level path main() touches into the temp tree.
        self._saved = {
            k: getattr(tg, k)
            for k in (
                "REPO", "TASKS_DIR", "RESULTS_DIR", "STATE_DIR",
                "ARCHIVE_TASKS_DIR", "ARCHIVE_RESULTS_DIR", "ACCESS_FILE",
                "api", "send_reply", "_emit_channel", "load_allowed",
                "presenter_mode_active", "_single_instance_acquire",
                "_recover_orphan_sending_files", "time",
            )
        }
        tg.REPO = self.tmp
        tg.TASKS_DIR = self.tmp / "tasks"
        tg.RESULTS_DIR = self.tmp / "results"
        tg.STATE_DIR = self.tmp / "state"
        tg.ARCHIVE_TASKS_DIR = self.tmp / "tasks" / "archive"
        tg.ARCHIVE_RESULTS_DIR = self.tmp / "results" / "archive"
        tg.ACCESS_FILE = self.tmp / "access.json"
        tg.ACCESS_FILE.write_text('{"allowFrom": ["1"]}')  # skip the TOFU branch
        tg._single_instance_acquire = lambda name: None
        tg._recover_orphan_sending_files = lambda: 0
        tg.load_allowed = lambda: {"1"}
        tg.presenter_mode_active = lambda: True  # skip the proactive scan
        tg._emit_channel = lambda *a, **k: None
        self.api_calls = []
        self.sent = []

        def fake_api(method, **params):
            self.api_calls.append((method, params))
            if method == "getUpdates":
                return {"ok": True, "result": []}  # no new inbound messages
            return {"ok": True}

        def fake_send_reply(chat_id, text, task_id=None):
            self.sent.append((chat_id, text, task_id))
            return {"ok": True, "text_chunks": 1, "files_sent": 0}

        tg.api = fake_api
        tg.send_reply = fake_send_reply
        tg.time = _TimeShim(real_time)
        # Fresh process state — the restart wiped these.
        tg._progress_msgs.clear()
        tg.pending_task_tiers.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(tg, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_one_pass(self):
        with self.assertRaises(_StopLoop):
            tg.main()

    def test_orphaned_result_is_delivered_after_restart(self):
        tid = "task-1752700000001"
        (self.tmp / "tasks" / f"{tid}.txt").write_text(
            TG_TASK.format(task_id=tid, chat_id=self.CHAT_ID)
        )
        # A non-telegram task sits alongside — must be ignored entirely.
        (self.tmp / "tasks" / "task-chat-9.txt").write_text(
            "id: task-chat-9\nsource: chat\nchannel_id: local-chat\ntask: hi\n"
        )
        # Malformed junk must not break startup seeding.
        (self.tmp / "tasks" / "task-junk.txt").write_bytes(b"\x00garbage")
        # The core's reply landed while (or after) the bridge was down.
        (self.tmp / "results" / f"{tid}.txt").write_text("All done.")
        (self.tmp / "results" / "task-chat-9.txt").write_text("chat result — not ours")

        self._run_one_pass()

        # Delivered through the real delivery path to the right chat.
        self.assertEqual(self.sent, [(self.CHAT_ID, "All done.", tid)])
        # Both files archived (delivery bookkeeping ran).
        self.assertFalse((self.tmp / "tasks" / f"{tid}.txt").exists())
        self.assertFalse((self.tmp / "results" / f"{tid}.txt").exists())
        self.assertTrue(list((self.tmp / "results" / "archive").rglob(f"{tid}.txt")))
        self.assertTrue(list((self.tmp / "tasks" / "archive").rglob(f"{tid}.txt")))
        # The chat-sourced task/result pair was left alone for its own consumer.
        self.assertTrue((self.tmp / "tasks" / "task-chat-9.txt").exists())
        self.assertTrue((self.tmp / "results" / "task-chat-9.txt").exists())

    def test_recovered_claimed_task_delivers_and_archives(self):
        tid = "task-1752700000002"
        (self.tmp / "tasks" / f"{tid}.claimed-core-1.txt").write_text(
            TG_TASK.format(task_id=tid, chat_id=self.CHAT_ID)
        )
        (self.tmp / "results" / f"{tid}.txt").write_text("Claimed-task reply")

        self._run_one_pass()

        self.assertEqual(self.sent, [(self.CHAT_ID, "Claimed-task reply", tid)])
        self.assertFalse((self.tmp / "tasks" / f"{tid}.claimed-core-1.txt").exists())

    def test_recovered_no_send_result_goes_through_marker_path(self):
        # Rebuilt entries must run the IDENTICAL delivery code, markers included:
        # a [no-send] result is silently archived, never sent.
        tid = "task-1752700000003"
        (self.tmp / "tasks" / f"{tid}.txt").write_text(
            TG_TASK.format(task_id=tid, chat_id=self.CHAT_ID)
        )
        (self.tmp / "results" / f"{tid}.txt").write_text("[no-send]\ninternal only")

        self._run_one_pass()

        self.assertEqual(self.sent, [])
        self.assertFalse((self.tmp / "results" / f"{tid}.txt").exists())
        self.assertFalse((self.tmp / "tasks" / f"{tid}.txt").exists())
        self.assertTrue(list((self.tmp / "results" / "archive").rglob(f"{tid}.txt")))

    def test_no_result_yet_stays_pending_without_side_effects(self):
        # Restart recovery of a task whose result hasn't landed: nothing sent,
        # task file untouched — it just waits like a freshly-ingested task.
        tid = "task-1752700000004"
        (self.tmp / "tasks" / f"{tid}.txt").write_text(
            TG_TASK.format(task_id=tid, chat_id=self.CHAT_ID)
        )

        self._run_one_pass()

        self.assertEqual(self.sent, [])
        self.assertTrue((self.tmp / "tasks" / f"{tid}.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""A confirmed Discord task result stays one logical external delivery."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(os.environ.get("REPO_UNDER_TEST") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(REPO / "src"))

ROOT = Path(tempfile.mkdtemp(prefix="discord-result-idempotency-"))
CONFIG = tempfile.mkdtemp(prefix="discord-result-idempotency-config-")
WORKSPACE = ROOT / "workspace"
os.environ["CLAUDE_CONFIG_DIR"] = CONFIG
os.environ["SUTANDO_WORKSPACE"] = str(WORKSPACE)
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")
(Path(CONFIG) / "channels" / "discord").mkdir(parents=True)
(Path(CONFIG) / "channels" / "discord" / "access.json").write_text(
    json.dumps({"allowFrom": []}), encoding="utf-8")


def _install_discord_stub() -> None:
    discord = types.ModuleType("discord")

    class Intents:
        @staticmethod
        def default():
            return types.SimpleNamespace(message_content=False, members=False)

    class Client:
        def __init__(self, **_kwargs):
            self.loop = types.SimpleNamespace(create_task=lambda *_a, **_k: None)

        @staticmethod
        def event(fn):
            return fn

    class MessageReference:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    discord.Intents = Intents
    discord.Client = Client
    discord.MessageReference = MessageReference
    discord.AllowedMentions = type("AllowedMentions", (), {})
    discord.Message = type("Message", (), {})
    discord.Thread = type("Thread", (), {})
    discord.DMChannel = type("DMChannel", (), {})
    discord.File = type("File", (), {"__init__": lambda self, *_a, **_k: None})
    sys.modules["discord"] = discord


_install_discord_stub()


def _load_bridge(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, REPO / "src" / "discord-bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge("discord_recreated_result_bridge")

CHANNEL_ID = "123456789012345678"


class StopPoll(Exception):
    pass


class Channel:
    def __init__(self):
        self.id = int(CHANNEL_ID)
        self.name = "idempotency-test"
        self.sent: list[str] = []

    async def send(self, content=None, **_kwargs):
        self.sent.append(str(content))
        return types.SimpleNamespace(id=len(self.sent))


class Client:
    def __init__(self, channel: Channel):
        self.channel = channel
        self.fetches = 0

    @staticmethod
    def is_ready():
        return False

    async def fetch_channel(self, channel_id):
        self.fetches += 1
        if int(channel_id) != self.channel.id:
            raise AssertionError(f"unexpected channel {channel_id}")
        return self.channel

    @staticmethod
    def get_channel(_channel_id):
        return None


class DiscordRecreatedResultTest(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(WORKSPACE, ignore_errors=True)
        for rel in ("results", "results/archive", "tasks", "tasks/archive", "state", "logs"):
            (WORKSPACE / rel).mkdir(parents=True, exist_ok=True)
        bridge.REPO = WORKSPACE
        bridge.RESULTS_DIR = WORKSPACE / "results"
        bridge.TASKS_DIR = WORKSPACE / "tasks"
        bridge.ARCHIVE_RESULTS_DIR = bridge.RESULTS_DIR / "archive"
        bridge.ARCHIVE_TASKS_DIR = bridge.TASKS_DIR / "archive"
        bridge.STATE_DIR = WORKSPACE / "state"
        bridge.DELIVERED_DIR = bridge.STATE_DIR / "discord-delivered"
        bridge.PENDING_REPLIES_FILE = bridge.STATE_DIR / "pending-discord-replies.json"
        bridge.pending_replies.clear()
        bridge.pending_reply_anchors.clear()
        bridge.pending_task_tiers.clear()
        bridge._empty_result_polls.clear()
        bridge._progress_msgs.clear()
        bridge._recovered_replies = {}
        bridge._orphan_route_cursor = ""
        notices = getattr(bridge, "_ambiguous_receipt_notices", None)
        if notices is not None:
            notices.clear()
            bridge._ambiguous_receipt_notice_overflow = False
        bridge.save_pending_replies = lambda: None
        bridge.result_audit._audit_path = lambda: WORKSPACE / "state" / "result-audit.log"
        self.channel = Channel()
        self.client = Client(self.channel)
        bridge.client = self.client

    @staticmethod
    def _archived_task(task_id: str) -> None:
        month = bridge.ARCHIVE_TASKS_DIR / "2026-08"
        month.mkdir(parents=True, exist_ok=True)
        (month / f"{task_id}.txt").write_text(
            f"id: {task_id}\nsource: discord\nchannel_id: {CHANNEL_ID}\n"
            "access_tier: owner\ntask: test\n",
            encoding="utf-8",
        )

    @staticmethod
    def _live_task(task_id: str) -> None:
        (bridge.TASKS_DIR / f"{task_id}.txt").write_text(
            f"id: {task_id}\nsource: discord\nchannel_id: {CHANNEL_ID}\n"
            "access_tier: owner\ntask: test\n",
            encoding="utf-8",
        )

    @staticmethod
    def _result(task_id: str, body: str) -> None:
        (bridge.RESULTS_DIR / f"{task_id}.txt").write_text(body, encoding="utf-8")

    @staticmethod
    def _terminal(task_id: str, disposition=None) -> None:
        bridge.outbox.record_terminal_receipt(
            bridge._result_receipt_root(), task_id,
            disposition or bridge.outbox.TerminalDisposition.DELIVERED,
        )

    @staticmethod
    def _archive_bodies(task_id: str) -> list[str]:
        return sorted(
            p.read_text(encoding="utf-8")
            for p in bridge.ARCHIVE_RESULTS_DIR.glob(f"*/{task_id}.txt*"))

    @staticmethod
    def _audit_rows(task_id: str) -> list[list[str]]:
        audit = WORKSPACE / "state" / "result-audit.log"
        if not audit.exists():
            return []
        return [
            fields
            for line in audit.read_text(encoding="utf-8").splitlines()
            if len(fields := line.split("\t")) == 4 and fields[1] == task_id
        ]

    @staticmethod
    def _one_pass(module=bridge) -> None:
        async def stop(_seconds):
            raise StopPoll()

        original = module.asyncio.sleep
        module.asyncio.sleep = stop
        try:
            with unittest.TestCase().assertRaises(StopPoll):
                asyncio.run(module.poll_results())
        finally:
            module.asyncio.sleep = original

    @staticmethod
    def _forget_runtime_routes() -> None:
        bridge.pending_replies.clear()
        bridge.pending_reply_anchors.clear()
        bridge.pending_task_tiers.clear()
        bridge._recovered_replies = {}
        bridge._orphan_route_cursor = ""

    def _seed_pending(self, task_id: str, body: str) -> None:
        self._live_task(task_id)
        self._result(task_id, body)
        bridge.pending_replies[task_id] = self.channel
        bridge.pending_reply_anchors[task_id] = 987654321098765432
        bridge.pending_task_tiers[task_id] = "owner"

    def _assert_pending_unknown_is_held(self, task_id: str, body: str) -> None:
        self._seed_pending(task_id, body)
        receipt_path = bridge.outbox._terminal_receipt_path(
            bridge._result_receipt_root(), task_id, 0)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{torn", encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for _ in range(3):
                self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertTrue((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertTrue((bridge.TASKS_DIR / f"{task_id}.txt").exists())
        self.assertIs(bridge.pending_replies.get(task_id), self.channel)
        self.assertEqual(
            bridge.pending_reply_anchors.get(task_id), 987654321098765432)
        self.assertEqual(bridge.pending_task_tiers.get(task_id), "owner")
        self.assertNotIn(task_id, bridge._empty_result_polls)
        self.assertEqual(self._audit_rows(task_id), [])
        self.assertEqual(
            output.getvalue().count(
                f"holding {task_id}: terminal outcome needs reconciliation"),
            1,
        )

    def test_recreated_identical_suppressed_but_changed_is_delivered(self):
        # Identical recreation is the double-send (suppressed); a recreation
        # whose content differs is a follow-up and must be delivered.
        task_id = "task-2000000000001"
        first = "first confirmed answer"
        changed = "completion narration written after the answer"
        self._archived_task(task_id)

        self._result(task_id, first)
        self._one_pass()
        self.assertEqual(self.channel.sent, [first])

        # Identical recreation -> suppressed (digest matches).
        self._forget_runtime_routes()
        self._result(task_id, first)
        self._one_pass()
        self.assertEqual(self.channel.sent, [first])

        # Changed recreation -> delivered as a follow-up (digest differs).
        self._forget_runtime_routes()
        self._result(task_id, changed)
        self._one_pass()

        self.assertEqual(self.channel.sent, [first, changed])
        self.assertFalse((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, bridge.pending_replies)
        # A second identical recreation of the follow-up is itself suppressed:
        # _mark_delivered re-receipted the new digest.
        self._forget_runtime_routes()
        self._result(task_id, changed)
        self._one_pass()
        self.assertEqual(self.channel.sent, [first, changed])

    def test_archive_failure_retries_without_send_or_audit_then_audits_once(self):
        task_id = "task-2000000000002"
        self._live_task(task_id)
        self._terminal(task_id)
        self._result(task_id, "late copy")
        original = bridge.archive_file
        bridge.archive_file = lambda *_args, **_kwargs: False
        try:
            for _ in range(3):
                self._one_pass()
        finally:
            bridge.archive_file = original

        self.assertEqual(self.channel.sent, [])
        self.assertTrue((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertTrue((bridge.TASKS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, bridge.pending_replies)
        self.assertEqual(self._audit_rows(task_id), [])

        self._one_pass()
        self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertFalse((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertFalse((bridge.TASKS_DIR / f"{task_id}.txt").exists())
        rows = self._audit_rows(task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:], ["deduped", "discord"])

    def test_pending_terminal_nonempty_result_retires_without_send_or_failure(self):
        task_id = "task-2000000000009"
        self._seed_pending(task_id, "already delivered")
        self._terminal(task_id)

        self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertFalse((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertFalse((bridge.TASKS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, bridge.pending_replies)
        self.assertNotIn(task_id, bridge.pending_reply_anchors)
        self.assertNotIn(task_id, bridge.pending_task_tiers)
        self.assertNotIn(task_id, bridge._empty_result_polls)
        rows = self._audit_rows(task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:], ["deduped", "discord"])

    def test_pending_terminal_empty_result_retires_without_send_or_failure(self):
        task_id = "task-2000000000010"
        self._seed_pending(task_id, "")
        self._terminal(task_id)

        self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertFalse((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertFalse((bridge.TASKS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, bridge.pending_replies)
        self.assertNotIn(task_id, bridge.pending_reply_anchors)
        self.assertNotIn(task_id, bridge.pending_task_tiers)
        self.assertNotIn(task_id, bridge._empty_result_polls)
        rows = self._audit_rows(task_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2:], ["deduped", "discord"])

    def test_pending_unknown_nonempty_result_stays_live_across_polls(self):
        self._assert_pending_unknown_is_held(
            "task-2000000000011", "must wait for receipt repair")

    def test_pending_unknown_empty_result_stays_live_without_empty_count(self):
        self._assert_pending_unknown_is_held("task-2000000000012", "")

    def test_fresh_bridge_module_suppresses_a_recreated_result(self):
        task_id = "task-2000000000013"
        first = "first process delivered this"
        self._archived_task(task_id)
        self._result(task_id, first)
        self._one_pass()
        self.assertEqual(self.channel.sent, [first])
        self.assertTrue(bridge._has_durable_terminal_receipt(task_id))
        self.assertFalse(bridge._delivered_sentinel_path(task_id).exists())

        # Identical content recreated after a restart is a suppressed
        # double-send (its digest matches the receipt).
        self._result(task_id, first)
        fresh = _load_bridge("discord_recreated_result_bridge_restart")
        fresh.REPO = WORKSPACE
        fresh.RESULTS_DIR = WORKSPACE / "results"
        fresh.TASKS_DIR = WORKSPACE / "tasks"
        fresh.ARCHIVE_RESULTS_DIR = fresh.RESULTS_DIR / "archive"
        fresh.ARCHIVE_TASKS_DIR = fresh.TASKS_DIR / "archive"
        fresh.STATE_DIR = WORKSPACE / "state"
        fresh.DELIVERED_DIR = fresh.STATE_DIR / "discord-delivered"
        fresh.PENDING_REPLIES_FILE = fresh.STATE_DIR / "pending-discord-replies.json"
        fresh.pending_replies.clear()
        fresh.pending_reply_anchors.clear()
        fresh.pending_task_tiers.clear()
        fresh._empty_result_polls.clear()
        fresh._progress_msgs.clear()
        fresh._recovered_replies = {}
        fresh._orphan_route_cursor = ""
        fresh._ambiguous_receipt_notices.clear()
        fresh._ambiguous_receipt_notice_overflow = False
        fresh.save_pending_replies = lambda: None
        fresh.result_audit._audit_path = lambda: WORKSPACE / "state" / "result-audit.log"
        fresh_channel = Channel()
        fresh_client = Client(fresh_channel)
        fresh.client = fresh_client

        self._one_pass(fresh)

        self.assertEqual(fresh_channel.sent, [])
        self.assertEqual(fresh_client.fetches, 0)
        self.assertFalse((fresh.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, fresh.pending_replies)
        self.assertTrue(fresh._has_durable_terminal_receipt(task_id))

    def test_failed_delivery_record_does_not_suppress_a_recreated_result(self):
        task_id = "task-2000000000003"
        self._archived_task(task_id)
        archived = bridge.ARCHIVE_RESULTS_DIR / "2026-08"
        archived.mkdir(parents=True, exist_ok=True)
        (archived / f"{task_id}.txt").write_text("failed attempt", encoding="utf-8")
        bridge.result_audit.record(task_id, "failed", "discord",
                                   ts="2026-08-19T18:00:00Z")

        self._result(task_id, "retry after failure")
        self._one_pass()

        self.assertEqual(self.channel.sent, ["retry after failure"])

    def test_other_surface_delivery_does_not_suppress_discord(self):
        task_id = "task-2000000000004"
        self._archived_task(task_id)
        bridge.outbox.record_terminal_receipt(
            WORKSPACE / "results" / ".outbox-slack-task-results",
            task_id,
            bridge.outbox.TerminalDisposition.DELIVERED,
        )
        self._result(task_id, "discord answer")

        self._one_pass()

        self.assertEqual(self.channel.sent, ["discord answer"])

    def test_legacy_only_sentinel_holds_live_pair_for_reconciliation(self):
        task_id = "task-2000000000005"
        task = bridge.TASKS_DIR / f"{task_id}.txt"
        task.write_text(
            f"id: {task_id}\nsource: discord\nchannel_id: {CHANNEL_ID}\n"
            "access_tier: owner\ntask: test\n",
            encoding="utf-8",
        )
        bridge.DELIVERED_DIR.mkdir(parents=True, exist_ok=True)
        bridge._delivered_sentinel_path(task_id).touch()
        self._result(task_id, "already sent before restart")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for _ in range(3):
                self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.client.fetches, 0)
        self.assertTrue(task.exists())
        self.assertTrue((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertTrue(bridge._delivered_sentinel_path(task_id).exists())
        self.assertFalse(bridge._has_durable_terminal_receipt(task_id))
        receipt = bridge.outbox.read_terminal_receipt(
            bridge._result_receipt_root(), task_id)
        self.assertIs(receipt.state, bridge.outbox.TerminalReceiptState.ABSENT)
        self.assertEqual(self._audit_rows(task_id), [])
        self.assertEqual(
            output.getvalue().count(
                f"holding {task_id}: terminal outcome needs reconciliation"),
            1,
        )

    def test_terminal_skip_outcomes_do_not_resurrect_as_plain_replies(self):
        for offset, marker, disposition in (
            (6, "[no-send]\nalready handled", bridge.outbox.TerminalDisposition.NO_SEND),
            (7, "[deduped: task-holder]\nalready handled",
             bridge.outbox.TerminalDisposition.DEDUPED),
        ):
            with self.subTest(disposition=disposition.value):
                task_id = f"task-200000000000{offset}"
                self._live_task(task_id)
                self._result(task_id, marker)
                self._one_pass()
                receipt = bridge.outbox.read_terminal_receipt(
                    bridge._result_receipt_root(), task_id)
                self.assertEqual(receipt.disposition, disposition)

                self._forget_runtime_routes()
                self._result(task_id, "late completion narration")
                self._one_pass()

        self.assertEqual(self.channel.sent, [])

    def test_corrupt_receipt_holds_the_result_instead_of_sending_or_discarding(self):
        task_id = "task-2000000000008"
        self._archived_task(task_id)
        receipt_path = bridge.outbox._terminal_receipt_path(
            bridge._result_receipt_root(), task_id, 0)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text("{torn", encoding="utf-8")
        self._result(task_id, "must wait for operator repair")

        self._one_pass()

        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.client.fetches, 0)
        self.assertTrue((bridge.RESULTS_DIR / f"{task_id}.txt").exists())
        self.assertNotIn(task_id, bridge.pending_replies)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for _recover_orphaned_task_routing() / _gather_pending_task_ids() in
src/telegram-bridge.py.

Bug: pending_replies (task_id -> chat_id) is memory-only, local to main().
If the bridge process restarts after writing a task but before its result
arrives, the new process's pending_replies is empty, so the "Check for
results to send back" loop never sees that task_id again — the result file
sits in results/ forever, un-delivered and silently orphaned. Same bug class
already fixed in src/slack-bridge.py (see
tests/slack-bridge-orphaned-routing-recovery.test.py) — this is the Telegram
counterpart, found by grepping for the same pattern after that fix landed.

Fix: chat_id is durable — it's in the task file's own headers from creation
time (`f"chat_id: {chat_id}\n"` at task-write time) — even though
pending_replies isn't. _recover_orphaned_task_routing() rebuilds routing for
any orphaned result by re-reading the original task file.

Run: python3 tests/telegram-bridge-orphaned-routing-recovery.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_bridge(workspace: Path):
    """Load telegram-bridge.py with a temp workspace. Each call does a fresh
    module_from_spec + exec_module, so two calls model two independent OS
    processes (module globals — including any dict a test creates fresh —
    never carry over)."""
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_routing_recovery_under_test", REPO / "src" / "telegram-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_task(path: Path, **headers) -> None:
    lines = [f"{k}: {v}" for k, v in headers.items() if v is not None]
    lines.append("task: do the thing")
    path.write_text("\n".join(lines) + "\n")


class TestRecoverOrphanedTaskRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tg-routing-recovery-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        self.mod = _load_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_orphaned_telegram_task_is_recovered(self):
        _write_task(self.tasks / "task-111.txt", id="task-111", source="telegram", chat_id="123456")
        (self.results / "task-111.txt").write_text("the answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {"task-111": 123456})

    def test_chat_id_recovered_as_int(self):
        """chat_id must come back as int — Telegram's api() rejects a str chat_id."""
        _write_task(self.tasks / "task-222.txt", id="task-222", source="telegram", chat_id="-987654321")
        (self.results / "task-222.txt").write_text("group chat answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered["task-222"], -987654321)
        self.assertIsInstance(recovered["task-222"], int)

    def test_already_known_task_id_is_not_recovered(self):
        _write_task(self.tasks / "task-333.txt", id="task-333", source="telegram", chat_id="1")
        (self.results / "task-333.txt").write_text("normal delivery")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, {"task-333"})
        self.assertEqual(recovered, {})

    def test_non_telegram_task_is_left_for_its_own_bridge(self):
        """A stranded slack/discord result must not be claimed here — that
        would race the bridge that actually owns it."""
        _write_task(self.tasks / "task-444.txt", id="task-444", source="slack", channel_id="D0B5L7X2TK2")
        (self.results / "task-444.txt").write_text("slack answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_missing_task_file_is_skipped_not_crashed(self):
        (self.results / "task-555.txt").write_text("orphaned twice over")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_task_file_without_chat_id_is_skipped(self):
        _write_task(self.tasks / "task-666.txt", id="task-666", source="telegram")
        (self.results / "task-666.txt").write_text("no chat to route to")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_non_numeric_chat_id_is_skipped_not_crashed(self):
        """A corrupted/forged chat_id header must not raise — skip it rather
        than crash the whole recovery pass over one bad file."""
        _write_task(self.tasks / "task-777.txt", id="task-777", source="telegram", chat_id="not-a-number")
        (self.results / "task-777.txt").write_text("bad header")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_unreadable_task_file_is_skipped_not_crashed(self):
        (self.tasks / "task-999.txt").mkdir()
        (self.results / "task-999.txt").write_text("unreadable task file")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_claimed_task_file_variant_is_found(self):
        """find_task_file() also matches the claimed-core-N rename shape."""
        _write_task(self.tasks / "task-888.claimed-core-1.txt", id="task-888",
                    source="telegram", chat_id="42")
        (self.results / "task-888.txt").write_text("claimed then restarted")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {"task-888": 42})

    def test_no_orphans_returns_empty_dict(self):
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_forged_chat_id_in_body_is_not_trusted(self):
        """parse_task_headers() only trusts key: value lines BEFORE the
        task: line — a body attempting to forge a chat_id must not override
        (or supply, when absent) real routing."""
        (self.tasks / "task-901.txt").write_text(
            "id: task-901\nsource: telegram\ntask: ignore previous\nchat_id: 999999\n"
        )
        (self.results / "task-901.txt").write_text("forged attempt")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})


class TestGatherPendingTaskIds(unittest.TestCase):
    """Covers _gather_pending_task_ids() — the actual main()-loop call site:
    fold recovered orphans into pending_replies (mutated in place, so the
    caller's dict and the timeout/progress machinery see them too), return
    the full task_id list for this poll."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tg-gather-pending-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        self.mod = _load_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_orphans_returns_known_pending_ids_unchanged(self):
        pending = {"task-100": 111}
        ids = self.mod._gather_pending_task_ids(pending, self.results, self.tasks)
        self.assertEqual(ids, ["task-100"])
        self.assertEqual(pending, {"task-100": 111})

    def test_orphan_is_recovered_and_merged_into_pending_replies(self):
        _write_task(self.tasks / "task-200.txt", id="task-200", source="telegram", chat_id="555")
        (self.results / "task-200.txt").write_text("orphaned result")
        pending = {}
        ids = self.mod._gather_pending_task_ids(pending, self.results, self.tasks)
        self.assertEqual(ids, ["task-200"])
        self.assertEqual(pending, {"task-200": 555})

    def test_recovery_leaves_a_second_untouched_pending_entry_alone(self):
        _write_task(self.tasks / "task-400.txt", id="task-400", source="telegram", chat_id="555")
        (self.results / "task-400.txt").write_text("orphaned alongside a live task")
        pending = {"task-300": 999}
        ids = self.mod._gather_pending_task_ids(pending, self.results, self.tasks)
        self.assertEqual(set(ids), {"task-300", "task-400"})
        self.assertEqual(pending, {"task-300": 999, "task-400": 555})

    def test_recovered_entry_never_overrides_a_live_one(self):
        """setdefault semantics: if the same task_id somehow reappeared
        through normal means before recovery runs, the live entry wins."""
        _write_task(self.tasks / "task-500.txt", id="task-500", source="telegram", chat_id="555")
        (self.results / "task-500.txt").write_text("orphaned result")
        pending = {"task-500": 111}  # already live with a different chat_id
        self.mod._gather_pending_task_ids(pending, self.results, self.tasks)
        self.assertEqual(pending["task-500"], 111)


class TestEndToEndRestartRecovery(unittest.TestCase):
    """Behavioral repro (same rigor requested by review on the sibling
    src/slack-bridge.py fix, PR #2218): simulate an actual process restart —
    a second, independent module load with fresh globals — write the result
    only after the "restart", then drive the REAL recovery + REAL
    send_reply() (not a hand-rolled substitute) and confirm delivery,
    stubbing only the Telegram HTTP boundary (api())."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tg-e2e-restart-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        # send_reply()'s local `import outbox_log` would otherwise hit the
        # real outbox log at the real workspace path — stub it so this test
        # can't write outside its own temp dir. Saved/restored so other test
        # files sharing a process see the real module again.
        self._orig_outbox_log = sys.modules.get("outbox_log")
        sys.modules["outbox_log"] = types.SimpleNamespace(append=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._orig_outbox_log is not None:
            sys.modules["outbox_log"] = self._orig_outbox_log
        else:
            sys.modules.pop("outbox_log", None)

    def test_result_written_after_restart_is_delivered_via_real_send_reply(self):
        task_id = "task-e2e-restart-1"
        _write_task(self.tasks / f"{task_id}.txt", id=task_id, source="telegram", chat_id="778899")

        # --- "Process 1": task enqueued, core still working, no result yet ---
        proc1 = _load_bridge(self.tmp)
        proc1.RESULTS_DIR = self.results
        proc1.TASKS_DIR = self.tasks
        pending1 = {task_id: 778899}

        # --- Restart: brand-new module globals, exactly like a new process ---
        proc2 = _load_bridge(self.tmp)
        proc2.RESULTS_DIR = self.results
        proc2.TASKS_DIR = self.tasks
        pending2: dict = {}  # sanity: a fresh process starts with nothing

        # The result lands only now, AFTER the restart — the case that was
        # silently dropped before this fix.
        (self.results / f"{task_id}.txt").write_text("the drafted answer, post-restart")

        sent = []

        def _fake_api(method, **params):
            sent.append((method, params))
            return {"ok": True}

        proc2.api = _fake_api

        # Drive the REAL functions: recovery, then the real delivery call
        # (send_reply), exactly as the main()-loop call site does.
        ids = proc2._gather_pending_task_ids(pending2, proc2.RESULTS_DIR, proc2.TASKS_DIR)
        self.assertEqual(ids, [task_id])
        result_file = proc2.RESULTS_DIR / f"{task_id}.txt"
        reply_text = result_file.read_text().strip()
        self.assertTrue(reply_text)
        chat_id = pending2.pop(task_id)
        result = proc2.send_reply(chat_id, reply_text, task_id=task_id)

        self.assertTrue(result["ok"])
        self.assertEqual(len(sent), 1)
        method, params = sent[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(params["chat_id"], 778899)
        self.assertEqual(params["text"], "the drafted answer, post-restart")
        self.assertNotIn(task_id, pending2)


if __name__ == "__main__":
    unittest.main()

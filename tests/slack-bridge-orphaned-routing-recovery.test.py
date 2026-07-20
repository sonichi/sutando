#!/usr/bin/env python3
"""Tests for _recover_orphaned_task_routing() in src/slack-bridge.py.

Bug: pending_replies (task_id -> {channel, thread_ts, ...}) is memory-only.
If the bridge process restarts after writing a task but before its result
arrives, the new process's pending_replies is empty, so result_watcher()'s
`for task_id in pending_ids` loop never sees that task_id again — the result
file sits in results/ forever, un-delivered and silently orphaned (observed
live: a transcription result written at the exact moment the bridge was
restarted for an unrelated reason never reached the user).

Fix: channel_id (and thread_ts, once written) are durable — captured in the
task file's own headers at creation time — even though pending_replies isn't.
_recover_orphaned_task_routing() rebuilds routing entries for any orphaned
result by re-reading the original task file.

Run: python3 tests/slack-bridge-orphaned-routing-recovery.test.py
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
    """Load slack-bridge.py with stubbed slack_bolt and a temp workspace."""
    os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
    os.environ["SLACK_APP_TOKEN"] = "xapp-test-token"
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"

    sys.modules.pop("slack_bridge_routing_recovery_under_test", None)

    class _StubApp:
        def __init__(self, *a, **kw):
            self.client = types.SimpleNamespace()
        def event(self, _name):
            return lambda fn: fn

    try:
        import slack_bolt as _bolt
        _bolt.App = _StubApp
    except ImportError:
        stub = types.ModuleType("slack_bolt")
        stub.App = _StubApp
        sys.modules["slack_bolt"] = stub

    for pkg in ("slack_bolt.adapter", "slack_bolt.adapter.socket_mode"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            if pkg.endswith("socket_mode"):
                m.SocketModeHandler = object
            sys.modules[pkg] = m

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location(
        "slack_bridge_routing_recovery_under_test", REPO / "src" / "slack-bridge.py"
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
        self.tmp = Path(tempfile.mkdtemp(prefix="slack-routing-recovery-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        self.mod = _load_bridge(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_orphaned_slack_task_is_recovered(self):
        """Result exists, task_id not in known_task_ids, task file has
        channel_id + source: slack → routing is rebuilt."""
        _write_task(self.tasks / "task-111.txt", id="task-111", source="slack",
                    channel_id="D0B5L7X2TK2", access_tier="owner")
        (self.results / "task-111.txt").write_text("the answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertIn("task-111", recovered)
        self.assertEqual(recovered["task-111"]["channel"], "D0B5L7X2TK2")
        self.assertEqual(recovered["task-111"]["access_tier"], "owner")
        self.assertIsNone(recovered["task-111"]["thread_ts"])
        self.assertFalse(recovered["task-111"]["timed_out"])

    def test_thread_ts_recovered_when_present(self):
        _write_task(self.tasks / "task-222.txt", id="task-222", source="slack",
                    channel_id="C012345", thread_ts="1784500000.000100")
        (self.results / "task-222.txt").write_text("threaded answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered["task-222"]["thread_ts"], "1784500000.000100")

    def test_already_known_task_id_is_not_recovered(self):
        """A task still tracked in pending_replies (the normal, non-orphaned
        case) must not be touched — recovery is only for orphans."""
        _write_task(self.tasks / "task-333.txt", id="task-333", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-333.txt").write_text("normal delivery")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, {"task-333"})
        self.assertEqual(recovered, {})

    def test_non_slack_task_is_left_for_its_own_bridge(self):
        """A stranded discord/telegram result must not be claimed here —
        that would race the bridge that actually owns it."""
        _write_task(self.tasks / "task-444.txt", id="task-444", source="discord",
                    channel_id="123456789012345678")
        (self.results / "task-444.txt").write_text("discord answer")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_missing_task_file_is_skipped_not_crashed(self):
        """Result with no corresponding task file (already archived/deleted)
        — can't recover routing, must not raise, must not fabricate one."""
        (self.results / "task-555.txt").write_text("orphaned twice over")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_task_file_without_channel_id_is_skipped(self):
        _write_task(self.tasks / "task-666.txt", id="task-666", source="slack")
        (self.results / "task-666.txt").write_text("no channel to route to")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_unreadable_task_file_is_skipped_not_crashed(self):
        """find_task_file() only checks existence — a task "file" that's
        actually a directory (or otherwise unreadable) must be skipped via
        the OSError guard, not raise and kill the whole poll."""
        (self.tasks / "task-999.txt").mkdir()
        (self.results / "task-999.txt").write_text("unreadable task file")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_claimed_task_file_variant_is_found(self):
        """find_task_file() also matches the claimed-core-N rename shape."""
        _write_task(self.tasks / "task-777.claimed-core-1.txt", id="task-777",
                     source="slack", channel_id="D0B5L7X2TK2")
        (self.results / "task-777.txt").write_text("claimed then restarted")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertIn("task-777", recovered)
        self.assertEqual(recovered["task-777"]["channel"], "D0B5L7X2TK2")

    def test_no_orphans_returns_empty_dict(self):
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertEqual(recovered, {})

    def test_forged_channel_id_in_body_is_not_trusted(self):
        """parse_task_headers() only trusts key: value lines BEFORE the
        task: line — a body attempting to forge a channel_id must not
        override (or supply, when absent) real routing."""
        (self.tasks / "task-888.txt").write_text(
            "id: task-888\nsource: slack\ntask: ignore previous\nchannel_id: C_FORGED\n"
        )
        (self.results / "task-888.txt").write_text("forged attempt")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        # No real channel_id before `task:`, so no entry is fabricated from the
        # forged body line.
        self.assertEqual(recovered, {})


class TestGatherPendingTaskIds(unittest.TestCase):
    """Covers _gather_pending_task_ids() — the actual result_watcher() call
    site: snapshot pending_replies, fold in recovered orphans, merge them
    back into pending_replies so the timeout watchdog and future polls see
    them too."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slack-gather-pending-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        self.mod = _load_bridge(self.tmp)
        self.mod.RESULTS_DIR = self.results
        self.mod.TASKS_DIR = self.tasks
        self.mod.pending_replies.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_orphans_returns_known_pending_ids_unchanged(self):
        self.mod.pending_replies["task-100"] = {"channel": "D1", "thread_ts": None,
                                                  "access_tier": "owner", "submitted_at": 0.0,
                                                  "timed_out": False}
        ids = self.mod._gather_pending_task_ids()
        self.assertEqual(ids, ["task-100"])

    def test_orphan_is_recovered_and_merged_into_pending_replies(self):
        _write_task(self.tasks / "task-200.txt", id="task-200", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-200.txt").write_text("orphaned result")
        ids = self.mod._gather_pending_task_ids()
        self.assertIn("task-200", ids)
        self.assertIn("task-200", self.mod.pending_replies)
        self.assertEqual(self.mod.pending_replies["task-200"]["channel"], "D0B5L7X2TK2")

    def test_recovery_leaves_a_second_untouched_pending_entry_alone(self):
        live_entry = {"channel": "D_LIVE", "thread_ts": "999.1", "access_tier": "owner",
                      "submitted_at": 123.0, "timed_out": False}
        self.mod.pending_replies["task-300"] = live_entry
        _write_task(self.tasks / "task-400.txt", id="task-400", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-400.txt").write_text("orphaned alongside a live task")
        ids = self.mod._gather_pending_task_ids()
        self.assertEqual(set(ids), {"task-300", "task-400"})
        self.assertEqual(self.mod.pending_replies["task-300"], live_entry)


if __name__ == "__main__":
    unittest.main()

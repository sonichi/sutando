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

**Archive lookup matters.** @qingyun-wu's review on this PR's Telegram
counterpart (#2223) measured a real host and found that by the time a result
exists undelivered in results/, the task file has *already* moved to
tasks/archive/ in effectively every case — something other than this
bridge's own delivery-triggered archival moves it independently (most likely
task-orphan-check classifying the still-undelivered task as "done" on a
session restart, since its result already exists). Recovery scoped to live
tasks/ only would have returned {} on every real orphan. Fixed by falling
back to local_task_protocol.find_archived_task() (flat tasks/archive/,
tasks/processed/, and month-partitioned tasks/archive/YYYY-MM/) when the
live-tasks/ lookup misses.

**Recovery must run every tick, not once at startup** (correction from
@qingyun-wu's follow-up review, which caught a real remaining gap in an
earlier revision of this fix): a task can be created by the OLD process,
survive the restart, and have its result land AFTER the new process's
one-time startup scan — the core is still processing it at restart time.
A startup-only scan sees no result yet, never registers the task_id (it
wasn't created by *this* process), and nothing looks for it again once the
scan has passed. Confirmed as the exact mechanism behind the 26-file
live-host measurement above: the stranded replies were all tasks the
*previous* process had created, not new ones. See
TestResultWatcherEndToEndRestartRecovery below for the exact repro
qingyun-wu specified: write task, restart, start the real watcher loop
BEFORE the result exists, THEN write the result — assert delivery still
happens on a later tick.

Run: python3 tests/slack-bridge-orphaned-routing-recovery.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
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

    # --- Archive-lookup coverage (the actual common case per live measurement) ---

    def test_task_already_archived_flat_is_still_recovered(self):
        """The realistic case: by the time a result exists undelivered, the
        task file has already moved to the flat tasks/archive/ dir (not
        live tasks/) — measured on a real host, see module docstring."""
        archive = self.tasks / "archive"
        archive.mkdir(parents=True)
        _write_task(archive / "task-1010.txt", id="task-1010", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-1010.txt").write_text("already archived when orphaned")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertIn("task-1010", recovered)
        self.assertEqual(recovered["task-1010"]["channel"], "D0B5L7X2TK2")

    def test_task_already_archived_month_partitioned_is_still_recovered(self):
        """Bridge-written archives use tasks/archive/YYYY-MM/ (see
        archive_file() in this same module) — both archive shapes exist on
        a real host and must both be checked."""
        month_dir = self.tasks / "archive" / "2026-07"
        month_dir.mkdir(parents=True)
        _write_task(month_dir / "task-1011.txt", id="task-1011", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-1011.txt").write_text("archived in a month dir")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertIn("task-1011", recovered)
        self.assertEqual(recovered["task-1011"]["channel"], "D0B5L7X2TK2")

    def test_task_in_processed_dir_is_still_recovered(self):
        processed = self.tasks / "processed"
        processed.mkdir(parents=True)
        _write_task(processed / "task-1012.txt", id="task-1012", source="slack",
                    channel_id="D0B5L7X2TK2")
        (self.results / "task-1012.txt").write_text("in processed/")
        recovered = self.mod._recover_orphaned_task_routing(self.results, self.tasks, set())
        self.assertIn("task-1012", recovered)
        self.assertEqual(recovered["task-1012"]["channel"], "D0B5L7X2TK2")


class TestGatherPendingTaskIds(unittest.TestCase):
    """Covers _gather_pending_task_ids() — the actual result_watcher()
    per-tick call site: snapshot pending_replies, fold in recovered
    orphans, merge them back into pending_replies so the timeout watchdog
    and future polls see them too."""

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

    def test_no_result_yet_recovers_nothing_this_tick(self):
        """The task exists but its result hasn't landed yet — a tick before
        the result appears must not fabricate an entry, and must not error."""
        _write_task(self.tasks / "task-600.txt", id="task-600", source="slack",
                    channel_id="D0B5L7X2TK2")
        ids = self.mod._gather_pending_task_ids()
        self.assertEqual(ids, [])
        self.assertNotIn("task-600", self.mod.pending_replies)

    def test_orphan_recovered_on_a_later_tick_once_result_appears(self):
        """The exact gap @qingyun-wu's follow-up review caught: a task
        in-flight at restart time, whose result lands on a LATER tick, must
        still be recovered — not just orphans whose result already existed
        on the first post-restart tick."""
        _write_task(self.tasks / "task-700.txt", id="task-700", source="slack",
                    channel_id="D0B5L7X2TK2")
        # Tick 1 (right after "restart"): result hasn't landed yet.
        ids_tick1 = self.mod._gather_pending_task_ids()
        self.assertEqual(ids_tick1, [])
        self.assertNotIn("task-700", self.mod.pending_replies)
        # The core finishes processing sometime later, after that tick.
        (self.results / "task-700.txt").write_text("finished after the first post-restart tick")
        # Tick 2: must be recovered now.
        ids_tick2 = self.mod._gather_pending_task_ids()
        self.assertEqual(ids_tick2, ["task-700"])
        self.assertEqual(self.mod.pending_replies["task-700"]["channel"], "D0B5L7X2TK2")


class TestResultWatcherEndToEndRestartRecovery(unittest.TestCase):
    """Behavioral repro for @qingyun-wu's exact specified sequence: write the
    task, restart (fresh module = fresh pending_replies), start the REAL
    result_watcher() background loop BEFORE the result exists, THEN write
    the result — assert delivery still happens on a later tick, via the
    real send path (stubbed only at the Slack API boundary). This is the
    ordering an earlier revision of this fix got wrong (writing the result
    before the recovery pass is the one ordering where a startup-only scan
    happens to work — see the module docstring)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="slack-e2e-restart-test-"))
        self.results = self.tmp / "results"
        self.tasks = self.tmp / "tasks"
        self.results.mkdir(parents=True)
        self.tasks.mkdir(parents=True)
        # _send_reply()'s local `import result_audit` would otherwise hit
        # the real audit log at the real workspace path (resolve_workspace()
        # ignores SUTANDO_WORKSPACE as of v0.8) — stub it so this test can't
        # write outside its own temp dir. Saved/restored so other test files
        # sharing a process see the real module again.
        self._orig_result_audit = sys.modules.get("result_audit")
        sys.modules["result_audit"] = types.SimpleNamespace(record=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._orig_result_audit is not None:
            sys.modules["result_audit"] = self._orig_result_audit
        else:
            sys.modules.pop("result_audit", None)

    def _sandbox(self, mod):
        """Point every dir/file the delivery path touches at this test's
        temp dir. REPO/RESULTS_DIR/TASKS_DIR/ARCHIVE_*/PRESENTER_SENTINEL
        are all bound once at module-import time from the real
        resolve_workspace() (which ignores SUTANDO_WORKSPACE) — rebinding
        them post-load is required to keep the real result_watcher() loop
        from touching anything outside self.tmp. _emit_channel is neutered
        for the same reason (it's an observability sink with its own
        real-path resolution)."""
        mod.REPO = self.tmp
        mod.RESULTS_DIR = self.results
        mod.TASKS_DIR = self.tasks
        mod.ARCHIVE_TASKS_DIR = self.tasks / "archive"
        mod.ARCHIVE_RESULTS_DIR = self.results / "archive"
        mod.PRESENTER_SENTINEL = self.tmp / "state" / "presenter-mode.sentinel"
        mod._emit_channel = lambda *a, **k: None

    def test_result_written_after_restart_and_after_watcher_started_is_still_delivered(self):
        task_id = "task-e2e-restart-1"
        # Task file already archived (flat tasks/archive/) — the realistic
        # state by the time an orphaned result is found, per the live
        # measurement in the module docstring above. Exercising the easy
        # live-tasks/ case here would have silently passed against the
        # pre-fix code too (find_task_file() alone finds it there).
        archive = self.tasks / "archive"
        archive.mkdir(parents=True)
        _write_task(archive / f"{task_id}.txt", id=task_id, source="slack",
                    channel_id="D0E2ERESTART", access_tier="owner")

        # --- "Process 1": task enqueued, core still working, no result yet ---
        proc1 = _load_bridge(self.tmp)
        self._sandbox(proc1)
        with proc1.pending_replies_lock:
            proc1.pending_replies[task_id] = {
                "channel": "D0E2ERESTART", "thread_ts": None, "access_tier": "owner",
                "submitted_at": time.time(), "timed_out": False,
            }

        # --- Restart: brand-new module globals, exactly like a new process ---
        proc2 = _load_bridge(self.tmp)
        self._sandbox(proc2)

        # BEFORE: confirms the historical bug's precondition is real on this
        # fresh "process" — its pending_replies has no memory of the task at
        # all, which is exactly the state that used to drop the reply forever
        # once a result eventually landed.
        self.assertEqual(proc2.pending_replies, {}, "sanity: restart wiped in-memory routing")

        sent: list[dict] = []
        proc2.app.client.chat_postMessage = lambda **kw: sent.append(kw) or {"ok": True}

        # Start the REAL background loop — result_watcher() itself, not a
        # hand-rolled substitute — BEFORE the result exists. The core is
        # still "processing" at this point, exactly the sequence
        # @qingyun-wu specified.
        watcher_thread = threading.Thread(target=proc2.result_watcher, daemon=True)
        watcher_thread.start()

        # Give it a couple of ticks to prove it does NOT fabricate a
        # delivery while there's genuinely nothing to deliver yet.
        time.sleep(1.5)
        self.assertEqual(len(sent), 0, "must not send before the result exists")

        # The result lands only now — AFTER the restart AND after the
        # watcher has already been running for a bit.
        (self.results / f"{task_id}.txt").write_text("the drafted answer, post-restart")

        deadline = time.time() + 5
        while time.time() < deadline and not sent:
            time.sleep(0.05)

        # AFTER: the real watcher loop, driven only by durable on-disk state,
        # delivered the reply despite pending_replies having started empty
        # and the result having appeared well after the loop was already running.
        self.assertEqual(len(sent), 1, "result_watcher() never delivered the post-restart result")
        self.assertEqual(sent[0]["channel"], "D0E2ERESTART")
        self.assertEqual(sent[0]["text"], "the drafted answer, post-restart")

        # Delivered means popped + archived, not left to be re-sent forever.
        deadline = time.time() + 2
        while time.time() < deadline and task_id in proc2.pending_replies:
            time.sleep(0.05)
        self.assertNotIn(task_id, proc2.pending_replies)
        self.assertFalse((self.results / f"{task_id}.txt").exists())
        archived = list((self.results / "archive").rglob(f"{task_id}.txt"))
        self.assertEqual(len(archived), 1, "delivered result was not archived")


if __name__ == "__main__":
    unittest.main()

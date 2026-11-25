#!/usr/bin/env python3
"""The core's inbox while a limit hold keeps the core from launching (src/core_held_inbox.py).

- While held: each new task is offered to the switch-card intercept once; a usage-limit hold sends
  the account card for the owner's waiting messages (core_quota_notice); nothing is launched.
- Once the hold is lifted the core is launched through start-cli, at most once a minute; a launch
  refused again (75) keeps watching, a launch that starts the core ends the loop.
- start-cli.sh starts the loop on its exit-75 refusal.

Run: python3 tests/core-held-inbox.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import core_held_inbox as chi  # noqa: E402
import core_limit_hold as clh  # noqa: E402


class FakeQuota:
    def __init__(self):
        self.hits = []

    def hit(self, reset="", reset_at=None, tasks=(), credits=False):
        self.hits.append(list(tasks))
        return "quota-card"


class HeldInbox(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        (self.ws / "state").mkdir()
        self.t = time.time()
        self.launches = []
        self.intercepted = []
        self.quota = FakeQuota()
        self.rc = 0

    def tearDown(self):
        self.td.cleanup()

    def hold(self, **kw):
        rec = {"status": "held", "kind": "usage-limit", "started_at": self.t - 60, "until": self.t + 3600,
               "reset_at": self.t + 7200, "reason": "usage limit on haiku", "notice": "n.txt"}
        rec.update(kw)
        (self.ws / "state" / "core-limit-hold.json").write_text(json.dumps(rec))

    def task(self, name, tier="owner"):
        (self.ws / "tasks" / f"{name}.txt").write_text(
            f"id: {name}\ntimestamp: 2099-01-01T00:00:00Z\ntask: hi\naccess_tier: {tier}\n")

    def inbox(self):
        def launch(repo):
            self.launches.append(repo)
            return self.rc
        return chi.HeldInbox(self.ws, REPO, now=lambda: self.t, launch=launch, quota=self.quota,
                             intercept=lambda p: self.intercepted.append(p.name), log=lambda *a: None)

    def test_held_offers_each_task_once_and_cards_the_owner(self):
        self.hold()
        self.task("task-a")
        box = self.inbox()
        self.assertEqual(box.step(), "held")
        self.assertEqual(box.step(), "held")
        self.assertEqual(self.intercepted, ["task-a.txt"])
        self.assertEqual(self.quota.hits[0], ["task-a"])
        self.assertEqual(self.launches, [])

    def test_a_rate_limit_hold_sends_no_account_card(self):
        self.hold(kind="rate-limit")
        self.task("task-a")
        self.inbox().step()
        self.assertEqual(self.quota.hits, [])

    def test_lifted_launches_once_a_minute_until_up(self):
        self.hold(until=self.t - 1, reset_at=None)
        box = self.inbox()
        self.rc = chi.HOLD_EXIT
        self.assertEqual(box.step(), "held")
        self.assertEqual(box.step(), "waiting")
        self.assertEqual(len(self.launches), 1)
        self.t += chi.LAUNCH_EVERY_S + 1
        self.rc = 0
        self.assertEqual(box.step(), "launched")
        self.assertEqual(len(self.launches), 2)

    def test_no_hold_at_all_launches(self):
        self.assertIsNone(clh.read_hold(self.ws))
        self.assertEqual(self.inbox().step(), "launched")


class SameGateAsTheLauncher(HeldInbox):
    def test_a_hold_on_another_model_family_launches_like_the_launcher_does(self):
        from unittest import mock
        self.hold(model="claude-haiku-4-5")
        self.task("task-a")
        with mock.patch.object(clh, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None)):
            self.assertIsNone(clh.binding_hold(self.ws, self.t), "the launcher would start the core")
            self.assertEqual(self.inbox().step(), "launched")
        self.assertEqual((self.intercepted, self.quota.hits), ([], []), "no limit card for another model")
        self.assertEqual(len(self.launches), 1)

    def test_a_hold_on_the_same_family_still_holds(self):
        from unittest import mock
        self.hold(model="claude-haiku-4-5")
        self.task("task-a")
        with mock.patch.object(clh, "launch_identity", lambda ws, config_dir=None: ("claude-haiku-4-5", None)):
            self.assertEqual(self.inbox().step(), "held")
        self.assertEqual(self.launches, [])


class Edges(HeldInbox):
    def test_launch_core_returns_start_clis_code_and_one_when_it_cannot_run(self):
        import subprocess
        from unittest import mock
        with mock.patch.object(chi.subprocess, "run", return_value=subprocess.CompletedProcess([], 75)) as run:
            self.assertEqual(chi.launch_core(REPO), 75)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], str(REPO / "src" / "agent" / "start-cli.sh"))
        with mock.patch.object(chi.subprocess, "run", side_effect=OSError("no bash")), \
                mock.patch.object(chi, "_log") as log:
            self.assertEqual(chi.launch_core(REPO), 1)
        log.assert_called_once()

    def test_the_defaults_wire_the_quota_card_and_the_switch_intercept(self):
        from unittest import mock
        box = chi.HeldInbox(self.ws, REPO, launch=lambda r: 0, log=lambda *a: None)
        self.assertEqual(box.quota.runtime, "claude")
        with mock.patch("switch_card_intercept.intercept", return_value="no") as intercept:
            box.intercept(self.ws / "tasks" / "task-a.txt")
        self.assertEqual(intercept.call_args.args[1:3], (self.ws, self.ws / "results"))

    def test_an_unreadable_queue_and_a_failing_intercept_keep_the_tick_going(self):
        from unittest import mock
        self.hold()
        self.task("task-a")
        logged = []
        box = chi.HeldInbox(self.ws, REPO, now=lambda: self.t, launch=lambda r: 0, quota=self.quota,
                            intercept=lambda p: 1 / 0, log=logged.append)
        self.assertEqual(box.step(), "held")
        self.assertIn("task-a.txt: switch card failed (ZeroDivisionError)", logged)
        with mock.patch("task_queue.pending_files", side_effect=OSError):
            self.assertEqual(box.pending(), [])

    def test_a_failed_launch_is_logged_and_kept_watching(self):
        self.rc = 2
        self.assertEqual(self.inbox().step(), "failed")

    def test_main_runs_one_tick_under_the_lock_and_a_second_loop_stands_down(self):
        from unittest import mock
        with mock.patch.object(chi.HeldInbox, "step", return_value="held") as step, \
                mock.patch.object(chi, "_log"):
            self.assertEqual(chi.main(["--workspace", str(self.ws), "--once"]), 0)
            step.assert_called_once()
            held = chi._lock(self.ws)
            try:
                self.assertEqual(chi.main(["--workspace", str(self.ws), "--once"]), 0)
                step.assert_called_once()
            finally:
                held.close()

    def test_main_outlives_a_bad_tick_and_ends_once_the_core_is_up(self):
        from unittest import mock
        ticks = iter([RuntimeError("bad tick"), "launched"])

        def step(_self):
            got = next(ticks)
            if isinstance(got, Exception):
                raise got
            return got
        with mock.patch.object(chi.HeldInbox, "step", step), mock.patch.object(chi, "_log") as log, \
                mock.patch.object(chi.time, "sleep") as sleep, \
                mock.patch("workspace_default.resolve_workspace", return_value=str(self.ws)):
            self.assertEqual(chi.main([]), 0)
        sleep.assert_called_once()
        self.assertTrue(any("tick failed:" in c.args for c in log.call_args_list))


class Launcher(unittest.TestCase):
    def test_start_cli_starts_it_on_a_held_refusal(self):
        src = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text()
        at = src.index("src/core_held_inbox.py\" --repo")
        block = src[src.rindex('if [ "$_hold_rc" = 75 ]', 0, at):src.index("exit 75", at)]
        self.assertIn("nohup", block)


if __name__ == "__main__":
    unittest.main()

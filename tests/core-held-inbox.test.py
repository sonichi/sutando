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


class Launcher(unittest.TestCase):
    def test_start_cli_starts_it_on_a_held_refusal(self):
        src = (REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh").read_text()
        at = src.index("src/core_held_inbox.py\" --repo")
        block = src[src.rindex('if [ "$_hold_rc" = 75 ]', 0, at):src.index("exit 75", at)]
        self.assertIn("nohup", block)


if __name__ == "__main__":
    unittest.main()

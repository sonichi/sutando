#!/usr/bin/env python3
"""A named gateway instance's task id (`task-<instance>~<broker id>`) passes every task-id check
its tasks reach: the settled ledger, owner-agent-consult, the PR shepherd, status-ack's first-seen
record and the connect-apps pre-check. A plain id still passes, and a traversal still fails.

Run: python3 tests/lane-task-ids.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import local_task_protocol as ltp  # noqa: E402
import task_lifecycle  # noqa: E402

LANE = "task-ag2space_dlocal~task-9c65da61713b5c635f"
# The gateway's broker ids are not all `task-*`: a voice ask arrives as ask-<hex> or sc-ask-<hex>.
LANES = (LANE, "task-dev~ask-5f3c9e0a1b2c", "task-dev~sc-ask-5f3c9e0a1b2c", "task-dev~" + "b" * 64)
BAD = ("task-a~", "task-~task-1", "task-a~task-1~x", "task-a/b~task-1", "task-..~task-1", "task-a~../x",
       "task-a~..", "task-a~.", "task-a~" + "b" * 65, "task-a~a b", "task-a~a\x00",
       "task-" + "i" * 33 + "~task-1", None, 7)


def _load(rel: str, name: str):
    path = REPO / rel
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LaneTaskIds(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_the_shared_predicate_accepts_a_lane_id_and_nothing_looser(self):
        for lane in LANES:
            with self.subTest(lane=lane):
                self.assertTrue(ltp.valid_local_task_id(lane))
        self.assertTrue(ltp.valid_local_task_id("task-1793057400000"))
        self.assertFalse(ltp.valid_task_id(LANE), "the plain check stays strict")
        for bad in BAD:
            with self.subTest(bad=bad):
                self.assertFalse(ltp.valid_local_task_id(bad))

    def test_the_bridges_instance_rule_and_hold_check_are_the_shared_ones(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import gateway_instance, local_task_protocol as pkg_ltp, task_hold
        self.assertIs(gateway_instance.INSTANCE_RE, pkg_ltp.INSTANCE_ID_RE)
        self.assertTrue(task_hold.valid_tid(LANE))
        self.assertFalse(task_hold.valid_tid("task-a~../x"))

    def test_the_producer_and_every_consumer_share_one_rule(self):
        sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
        from ag2_sparrow import remote_gateway_bridge as gw, local_task_protocol as pkg_ltp
        self.assertIs(gw._TID_RE, pkg_ltp.BROKER_ID_RE, "the wire grammar is the shared one")
        for tid in LANES + ("task-1", "ask-5f3c9e0a1b2c"):
            with self.subTest(tid=tid):
                self.assertTrue(gw._valid_local_tid(tid))
        for bad in BAD[:-2]:
            with self.subTest(bad=bad):
                self.assertFalse(gw._valid_local_tid(bad))
        with mock.patch.object(pkg_ltp, "valid_local_task_id", return_value=False) as shared:
            self.assertFalse(gw._valid_local_tid("task-dev~ask-1"))
        shared.assert_called_once_with("task-dev~ask-1")

    def test_the_settled_ledger_appends_and_rereads_a_lane_id(self):
        path = self.ws / "settled.ids"
        task_lifecycle.SettledIds(path).add(LANE)
        self.assertIn(LANE, task_lifecycle.SettledIds(path).durable)
        with self.assertRaises(task_lifecycle.LedgerError):
            task_lifecycle.SettledIds(path).add("task-a~../x")

    def test_owner_agent_consult_takes_a_lane_task(self):
        consult = _load("skills/owner-agent-consult/scripts/consult_policy.py", "consult_policy_lane")
        (self.ws / "tasks").mkdir()
        (self.ws / "tasks" / f"{LANE}.txt").write_text(f"id: {LANE}\nsource: ag2space\ntask: ask\n")
        self.assertTrue(consult.task_can_post(self.ws, LANE))
        self.assertNotIn("is not a task id", consult.live_task(LANE, self.ws).get("reason", ""))

    def test_the_pr_shepherd_builds_a_contract_path_for_a_lane_task(self):
        shep = _load("skills/pr-shepherd-contract/scripts/shepherd_github.py", "shepherd_github_lane")
        with mock.patch.object(shep, "state_dir", return_value=self.ws):
            self.assertEqual(shep._contract_path(LANE), self.ws / f"{LANE}.json")
            with self.assertRaises(ValueError):
                shep._contract_path("task-a~../x")

    def test_status_ack_records_a_lane_tasks_first_seen(self):
        fs = _load("skills/status-ack/scripts/first_seen.py", "first_seen_lane")
        for lane in LANES:
            self.assertEqual(fs.record(self.ws, lane, 1000.0), 1000.0)
            self.assertEqual(fs.read(self.ws, lane), 1000.0)
        for bad in ("task-a~..", "task-a~task-1~x", "task-a~" + "b" * 65):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                fs.read(self.ws, bad)
        self.assertEqual(fs.record(self.ws, "task-chat-1.2_x", 5.0), 5.0, "plain ids as before")

    def test_the_activity_hook_binds_a_lane_task_and_refuses_a_bad_one(self):
        hook = _load("skills/agent-activity/hooks/activity-hook.py", "activity_hook_lane")
        for lane in LANES:
            self.assertEqual(hook.task_file_refs(f"cat /w/tasks/{lane}.txt"), [lane])
            self.assertEqual(hook.result_file_refs(f"/w/results/{lane}.txt"), [lane])
        self.assertEqual(hook.task_file_refs("cat /w/tasks/task-a~task-1~x.txt"), [])
        self.assertEqual(hook.task_file_refs("cat /w/tasks/task-1.txt"), ["task-1"])

    def test_a_cancel_names_the_whole_lane_id(self):
        import activity_bus
        for lane in LANES:
            self.assertEqual(activity_bus.cancel_target(f"CANCEL_INSTRUCTION: stop processing {lane} if still"), lane)
        self.assertIsNone(activity_bus.cancel_target("CANCEL_INSTRUCTION: stop processing task-a~task-1~x now"))
        self.assertEqual(activity_bus.cancel_target("CANCEL_INSTRUCTION: stop processing task-17 now"), "task-17")
        self.assertEqual(activity_bus.main(["transition", "CANCELLED", "--task-id", "task-a~..",
                                            "--workspace", str(self.ws)]), 2)

    def test_the_watcher_emit_takes_the_whole_lane_id(self):
        import subprocess
        tasks = self.ws / "tasks"
        tasks.mkdir()
        ctl = tasks / "task-ctl.txt"
        ctl.write_text("id: task-ctl\ntask: CANCEL_INSTRUCTION: stop processing task-dev~ask-5f3c9e0a1b2c now\n")
        stub, record = self.ws / "py", self.ws / "calls"
        stub.write_text(f'#!/bin/bash\necho "$*" >> "{record}"\n')
        stub.chmod(0o755)
        subprocess.run(["bash", "-c", f'source "{REPO}/src/task-emit.sh"; activity_cancel_target task-ctl.txt; wait'],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.ws), "SUTANDO_PY_BIN": str(stub), "TASKS_DIR": str(tasks)}, check=True)
        import time
        for _ in range(100):
            if record.exists() and "CANCELLED" in record.read_text():
                break
            time.sleep(0.05)
        self.assertIn("--task-id task-dev~ask-5f3c9e0a1b2c ", record.read_text())

    def test_the_connect_apps_precheck_sees_a_lane_task_file(self):
        pre = _load("skills/connect-apps/hooks/connect-precheck.py", "connect_precheck_lane")
        self.assertEqual([t for _, t in pre.TASK_FILE.findall(f"cat /w/tasks/{LANE}.txt")], [LANE])


if __name__ == "__main__":
    unittest.main()

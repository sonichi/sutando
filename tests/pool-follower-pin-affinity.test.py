#!/usr/bin/env python3
"""The leaderless fallback must honor an explicit pin.

Affinity is lead-side, so a dead lead used to drop it entirely and all workers
raced every task. A DEAD pin owner must still never stall the queue.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pool_follower as pf

def setup(td, pinned_to=None, owner_alive=None, pinned_flag=True):
    tasks=Path(td)/"tasks"; state=Path(td)/"state"
    (tasks).mkdir(); (state/"cores").mkdir(parents=True); (state/"pool").mkdir()
    (tasks/"task-a1.txt").write_text("id: task-a1\nchannel_id: !room:ag2.space\ntask: hi\n")
    if pinned_to:
        e={"instance":pinned_to,"ts":time.time()}
        if pinned_flag: e["pinned"]=True
        (state/"pool"/"affinity.json").write_text(json.dumps({"!room:ag2.space":e}))
    if owner_alive:
        (state/"cores"/f"{owner_alive}.alive").write_text("x")
    return tasks,state

class T(unittest.TestCase):
    def test_pinned_to_a_live_peer_is_left_alone(self):
        with TemporaryDirectory() as td:
            t,s=setup(td,pinned_to="core-2",owner_alive="core-2")
            self.assertIsNone(pf.acquire_work(t,s,"core-3","pool-lead"))
    def test_the_pinned_owner_itself_still_takes_it(self):
        with TemporaryDirectory() as td:
            t,s=setup(td,pinned_to="core-2",owner_alive="core-2")
            self.assertIsNotNone(pf.acquire_work(t,s,"core-2","pool-lead"))
    def test_a_DEAD_pin_owner_does_not_stall_the_queue(self):
        with TemporaryDirectory() as td:
            t,s=setup(td,pinned_to="core-2",owner_alive=None)
            got=pf.acquire_work(t,s,"core-3","pool-lead")
            self.assertIsNotNone(got,"a dead owner must not strand the task")
    def test_sticky_entry_without_pinned_true_does_not_block(self):
        with TemporaryDirectory() as td:
            t,s=setup(td,pinned_to="core-2",owner_alive="core-2",pinned_flag=False)
            self.assertIsNotNone(pf.acquire_work(t,s,"core-3","pool-lead"))
    def test_unpinned_task_unchanged(self):
        with TemporaryDirectory() as td:
            t,s=setup(td)
            self.assertIsNotNone(pf.acquire_work(t,s,"core-3","pool-lead"))
    def test_missing_affinity_file_is_not_fatal(self):
        with TemporaryDirectory() as td:
            t,s=setup(td); (s/"pool").rmdir()
            self.assertIsNotNone(pf.acquire_work(t,s,"core-3","pool-lead"))
if __name__=="__main__": unittest.main(verbosity=1)

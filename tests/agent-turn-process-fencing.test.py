#!/usr/bin/env python3
"""Offline process-boundary evaluation of turn fencing; run this file with Python 3.9+.

Real child processes call the production lock, close and reader APIs on temporary
state. This covers scheduled contention and holder death, not proxy dispatch,
simultaneous stale-lock recovery, PID reuse, backend delivery or power loss.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import agent_turn as at

DEADLINE = 15


def child(request):
    state = Path(request["state"])
    action = request["action"]
    if action == "inspect":
        turns = state / "turns"
        result = {
            "open": sorted(p.name for p in at.open_turns(state)),
            "contexts": sorted(p.name for p in turns.glob("*.json")),
            "fences": sorted(p.name for p in turns.glob("*.closed")),
            "selected": {task: (p.name if (p := at.open_turn_for_task(state, task)) else None)
                         for task in request["tasks"]},
            "closed": {turn: at.is_closed(state, turn) for turn in request["turns"]},
        }
    else:
        held = at._take_journal_lock(state) if action == "hold" else None
        if action == "hold" and held is None:
            raise RuntimeError("holder failed to acquire the production journal lock")
        ready = {"pid": os.getpid(), "lock": held[1] if held else None}
        Path(request["ready"]).write_text(json.dumps(ready))
        try:
            if sys.stdin.readline() != "go\n":
                raise RuntimeError("parent did not release the child barrier")
            result = {"closed": at.close_turn(state, request["turn"])} if action == "close" else {}
        finally:
            at._release_journal_lock(held)
    print(json.dumps(result), flush=True)


class TurnFencingProcesses(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sutando-turn-fencing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "first"
        self.other_state = self.root / "second"
        self.contexts = {}
        for state in (self.state, self.other_state):
            for task, generation, created in (("task_a", 1, 1000), ("task_a", 2, 2000),
                                               ("task_b", 1, 3000)):
                context = at.build_turn_context(task_id=task, execution_generation=generation,
                                                state_dir=state, now_ms=created)
                path = at.write_turn_context(state, context)
                self.contexts[state, task, generation] = path
        self.target = self.contexts[self.state, "task_a", 2]
        self.initial_bytes = {p: p.read_bytes() for p in self.contexts.values()}
        self.counter = 0

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    def start(self, action, state=None):
        self.counter += 1
        ready = self.root / f"ready-{self.counter}.json"
        chosen = self.state if state is None else state
        request = {"action": action, "state": str(chosen), "turn": self.target.stem,
                   "ready": str(ready), "tasks": ["task_a", "task_b"],
                   "turns": [p.stem for (s, _, _), p in self.contexts.items() if s == chosen]}
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child",
                                    json.dumps(request)], cwd=ROOT, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, process)
        if action != "inspect":
            deadline = time.monotonic() + DEADLINE
            while True:
                try:
                    message = json.loads(ready.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    message = None
                if message is not None:
                    self.assertEqual(message["pid"], process.pid)
                    self.assertIsNone(process.poll(), "child exited before reaching its barrier")
                    return process, message
                if process.poll() is not None:
                    out, err = process.communicate(timeout=DEADLINE)
                    self.fail(f"child exited before readiness: {process.returncode}: {out} {err}")
                if time.monotonic() >= deadline:
                    self.fail(f"child {action} did not reach its barrier")
                time.sleep(0.01)
        return process, None

    def collect(self, process, release=True):
        out, err = process.communicate(input="go\n" if release else None, timeout=DEADLINE)
        self.assertEqual(process.returncode, 0, f"child failed: {out} {err}")
        self.assertEqual(err, "")
        return json.loads(out)

    def close(self):
        process, _ = self.start("close")
        return self.collect(process)["closed"]

    def assert_state(self, closed=False):
        for state in (self.state, self.other_state):
            process, _ = self.start("inspect", state)
            observed = self.collect(process, release=False)
            paths = [p for (s, _, _), p in self.contexts.items() if s == state]
            removed = closed and state == self.state
            expected = sorted(p.name for p in paths if not (removed and p == self.target))
            self.assertEqual(observed["contexts"], expected)
            self.assertEqual(observed["open"], expected)
            self.assertEqual(observed["fences"], [f"{self.target.stem}.closed"] if removed else [])
            self.assertEqual(observed["closed"], {p.stem: removed and p == self.target for p in paths})
            selected = self.contexts[state, "task_a", 1 if removed else 2]
            self.assertEqual(observed["selected"], {"task_a": selected.name,
                                                    "task_b": self.contexts[state, "task_b", 1].name})
            for path in paths:
                if removed and path == self.target:
                    continue
                self.assertEqual(path.read_bytes(), self.initial_bytes[path])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        if closed:
            fence = self.target.with_suffix(".closed")
            self.assertEqual(fence.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(fence.stat().st_mode), 0o400)

    def test_live_holder_preserves_retry_state_then_release_allows_close(self):
        holder, ready = self.start("hold")
        lock = self.state / "room-actions.jsonl.lock"
        self.assertEqual(int(ready["lock"].split()[0]), holder.pid)
        original = lock.read_bytes()
        self.assertEqual(original.decode(), ready["lock"])
        self.assertFalse(self.close(), "a live independent holder must prevent fencing")
        self.assertIsNone(holder.poll())
        self.assertEqual(lock.read_bytes(), original, "the live holder's lock must remain intact")
        self.assert_state()
        self.collect(holder)
        self.assertFalse(lock.exists())
        self.assertTrue(self.close(), "the preserved turn must support a later close")
        self.assert_state(closed=True)
        self.assertFalse(lock.exists())

    def test_dead_holder_lock_is_recovered_by_a_new_process(self):
        holder, ready = self.start("hold")
        lock = self.state / "room-actions.jsonl.lock"
        self.assertEqual(int(ready["lock"].split()[0]), holder.pid)
        holder.kill()
        holder.wait(timeout=DEADLINE)
        self.assertNotEqual(holder.returncode, 0)
        self.assertEqual(lock.read_text(), ready["lock"], "death must leave a real orphaned lock")
        self.assert_state()
        self.assertTrue(self.close(), "a reaped holder's lock must not prevent fencing")
        self.assert_state(closed=True)
        self.assertFalse(lock.exists())

    def test_independent_closers_converge_on_one_fence(self):
        processes = [self.start("close")[0] for _ in range(4)]
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.flush()
        for process in processes:
            self.assertTrue(self.collect(process, release=False)["closed"])
        self.assert_state(closed=True)
        fence = self.target.with_suffix(".closed")
        identity = (fence.stat().st_ino, fence.stat().st_mtime_ns)
        self.assertTrue(self.close(), "a later close is still idempotent")
        self.assertEqual((fence.stat().st_ino, fence.stat().st_mtime_ns), identity)
        self.assert_state(closed=True)
        self.assertFalse((self.state / "room-actions.jsonl.lock").exists())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        child(json.loads(sys.argv[2]))
    else:
        unittest.main()

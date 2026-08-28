#!/usr/bin/env python3
"""Push mode: the results-watcher emits a no-id `task.result` notification per
NEW result to every subscriber, skips the boot-seeded backlog, and drops dead
writers without wedging. Tests the extracted one-pass step (_emit_new_results)
so it needs no live socket."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "runtime-api"))

from tasks_view import TasksView  # noqa: E402
import server as srv  # noqa: E402

from protocol import notification_frame  # noqa: E402


class _FakeWriter:
    def __init__(self, dead=False):
        self.frames = []
        self.dead = dead

    def write(self, b):
        if self.dead:
            raise ConnectionResetError()
        self.frames.append(b)

    async def drain(self):
        if self.dead:
            raise ConnectionResetError()


class PushModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.results = base / "results"
        self.results.mkdir()
        self.tv = TasksView(base / "tasks", self.results, "@me:x")
        self.srv = srv.RuntimeServer.__new__(srv.RuntimeServer)  # no full boot

    def tearDown(self):
        self.tmp.cleanup()

    async def test_pushes_new_skips_seed_drops_dead_no_repush(self):
        good, dead = _FakeWriter(), _FakeWriter(dead=True)
        self.srv._subscribers = {good, dead}
        (self.results / "task-rtapi-old.txt").write_text("old")     # pre-existing
        seen = {f.name for f in self.tv._result_files()}            # seeded

        (self.results / "task-rtapi-new.txt").write_text("hello stream")  # new
        await self.srv._emit_new_results(self.tv, seen)
        # good got exactly the new result, as a NO-ID notification
        self.assertEqual(len(good.frames), 1)
        msg = json.loads(good.frames[0])
        self.assertNotIn("id", msg)
        self.assertEqual(msg["method"], "task.result")
        self.assertEqual(msg["params"]["taskId"], "task-rtapi-new")
        self.assertEqual(msg["params"]["result"], "hello stream")
        # dead writer dropped, not wedging the pass
        self.assertNotIn(dead, self.srv._subscribers)
        # second pass: nothing new → no re-push of a seen result
        await self.srv._emit_new_results(self.tv, seen)
        self.assertEqual(len(good.frames), 1)

    async def test_boot_seed_skips_unready_and_still_suppresses_complete(self):
        # Exercises the REAL seeding inside _results_watcher, not a hand-copy:
        # an unready boot placeholder must stay pushable once it fills.
        os.environ["SUTANDO_RESULT_POLL_S"] = "0.01"
        self.addCleanup(os.environ.pop, "SUTANDO_RESULT_POLL_S", None)
        w = _FakeWriter()
        self.srv._subscribers = {w}
        self.srv.dispatcher = type("_D", (), {"tasks": self.tv})()
        (self.results / "task-rtapi-boot.txt").write_text("   ")      # unready
        (self.results / "task-rtapi-done.txt").write_text("ALREADY DONE")
        watcher = asyncio.create_task(self.srv._results_watcher())
        await asyncio.sleep(0.05)                                     # seed runs
        (self.results / "task-rtapi-boot.txt").write_text("BOOT ANSWER")
        await asyncio.sleep(0.10)                                     # poll pass
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        ids = [json.loads(f)["params"]["taskId"] for f in w.frames]
        self.assertIn("task-rtapi-boot", ids)      # filled placeholder pushed
        self.assertNotIn("task-rtapi-done", ids)   # complete-at-boot suppressed

    async def test_room_result_does_not_leak_into_stream(self):
        # Source isolation: a result from ANOTHER channel (non task-rtapi- id)
        # must NOT be pushed to runtime-api subscribers — the exact cross-channel
        good = _FakeWriter()
        self.srv._subscribers = {good}
        seen = {f.name for f in self.tv._result_files()}
        (self.results / "task-1234567890.txt").write_text("a room reply")
        await self.srv._emit_new_results(self.tv, seen)
        self.assertEqual(good.frames, [])  # not streamed

    def test_notification_frame_has_no_id(self):
        msg = json.loads(notification_frame("task.result", {"taskId": "t"}))
        self.assertNotIn("id", msg)
        self.assertEqual(msg["jsonrpc"], "2.0")
        self.assertEqual(msg["method"], "task.result")


if __name__ == "__main__":
    unittest.main(verbosity=2)

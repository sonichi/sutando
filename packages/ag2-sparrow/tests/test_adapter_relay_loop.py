"""The shared relay library (`ag2_sparrow/relay_loop.py`) and both Claude adapters' delegation to it.
The import and path boundaries are in test_adapter_boundaries.py.

Run:
    python3 packages/ag2-sparrow/tests/test_adapter_relay_loop.py
"""
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import relay_loop as rl, task_io, task_priority  # noqa: E402

from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402
from ag2_sparrow.adapters.hatch import task_fields  # noqa: E402

from ag2_sparrow.adapters.claude_cloud_session import session_adapter as sa  # noqa: E402
from ag2_sparrow.adapters.claude_routine import routine_adapter as ra  # noqa: E402

def write_task(inbox, tid, priority, mtime, tier="owner"):
    p = Path(inbox) / (tid + ".txt")
    p.write_text("id: %s\nchannel_id: !r:x\naccess_tier: %s\npriority: %s\ntask: hi\n" % (tid, tier, priority))
    os.utime(p, (mtime, mtime))


class Loop(rl.RelayLoop):
    def __init__(self, work, **kw):
        args = types.SimpleNamespace(inbox=os.path.join(work, "tasks"), results=os.path.join(work, "results"),
                                     state=os.path.join(work, "state"), max_per_hour=kw.get("max_per_hour", 5),
                                     max_retries=1, backoff=2.0, backoff_max=60.0, once=0, idle_exit=0, poll=0.01,
                                     allow_non_owner=kw.get("allow_non_owner", False))
        for d in (args.inbox, args.results, args.state):
            os.makedirs(d, exist_ok=True)
        self.init_loop(args, "sends.json", clock=lambda: 1000.0, sleep=lambda s: None)


class PriorityTest(unittest.TestCase):
    def test_urgent_before_normal_before_low_and_fifo_within_a_tier(self):
        with tempfile.TemporaryDirectory() as d:
            loop = Loop(d)
            write_task(loop.args.inbox, "task-low", "low", 100)
            write_task(loop.args.inbox, "task-n2", "normal", 300)
            write_task(loop.args.inbox, "task-n1", "normal", 200)
            write_task(loop.args.inbox, "task-urgent", "urgent", 400)
            write_task(loop.args.inbox, "task-bogus", "whenever", 250)
            (Path(loop.args.inbox) / "notes.txt").write_text("x")
            self.assertEqual(loop.pending(set()),
                             ["task-urgent.txt", "task-n1.txt", "task-bogus.txt", "task-n2.txt", "task-low.txt"])
            self.assertEqual(loop.pending({"task-urgent"}), ["task-n1.txt", "task-bogus.txt", "task-n2.txt",
                                                             "task-low.txt"])

    def test_answered_tasks_are_not_pending(self):
        with tempfile.TemporaryDirectory() as d:
            loop = Loop(d)
            write_task(loop.args.inbox, "task-a", "normal", 100)
            (Path(loop.args.results) / "task-a.txt").write_text("done")
            self.assertEqual(loop.pending(set()), [])

    def test_the_ordering_is_the_vendored_task_priority_policy(self):
        self.assertIs(rl.task_priority, task_priority)
        src = (PKG_ROOT.parents[1] / "src" / "task_priority.py")
        if src.exists():
            self.assertEqual(src.read_text(), (PKG_ROOT / "ag2_sparrow" / "task_priority.py").read_text())


class LimiterTest(unittest.TestCase):
    def test_keyed_entries_are_idempotent_persisted_and_forgettable(self):
        now = [5000.0]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sends.json")
            lim = rl.RateLimiter(path, 2, lambda: now[0])
            lim.record("task-a")
            lim.record("task-a")
            lim.record()
            self.assertEqual(len(lim.times), 2)
            again = rl.RateLimiter(path, 2, lambda: now[0])
            self.assertEqual(again.entries, [(5000.0, "task-a"), (5000.0, None)])
            again.forget("task-a")
            self.assertEqual(rl.RateLimiter(path, 2, lambda: now[0]).entries, [(5000.0, None)])
            Path(path).write_text("[[1]]")
            self.assertEqual(rl.RateLimiter(path, 2, lambda: now[0]).entries, [])


class LoopTest(unittest.TestCase):
    def test_read_task_admit_finish_and_retry(self):
        with tempfile.TemporaryDirectory() as d:
            loop = Loop(d)
            seen = set()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(loop.read_task("task-gone.txt", seen))
                self.assertIn("task-gone", seen)
                write_task(loop.args.inbox, "task-g", "normal", 100, tier="guest")
                headers, message, _ = loop.read_task("task-g.txt", seen)
                self.assertEqual((headers["access_tier"], message), ("guest", "hi"))
                self.assertFalse(loop.admit("task-g.txt", headers, seen))
                self.assertEqual((Path(loop.args.results) / "task-g.txt").read_text(), rl.NON_OWNER_REFUSED)
                self.assertTrue(loop.admit("task-o.txt", {"access_tier": "owner"}, seen))
                self.assertTrue(loop.schedule_retry("task-r", "x"))
                self.assertEqual(loop.not_before["task-r"], 1002.0)
                self.assertFalse(loop.schedule_retry("task-r", "x"), "max_retries is 1")
            loop.args.allow_non_owner = True
            self.assertTrue(loop.admit("task-t.txt", {"access_tier": "team"}, seen))
            self.assertEqual(Path(loop.done_path).read_text(), "task-g\n")
            with self.assertRaises(NotImplementedError):
                loop.step("task-x.txt", seen)

    def test_build_payload_extras_go_on_the_routing_line_only(self):
        _, text = rl.build_payload("t", {"access_tier": "owner", "channel_id": "!r:x"}, "m",
                                   extra=[("channel_kind", "room"), ("owner_dm", "!d:x y")])
        self.assertEqual(text.split("\n")[0],
                         "room=!r:x thread=none tier=owner collaborator=false task_id=t channel_kind=room owner_dm=!d:xy")
        _, plain = rl.build_payload("t", {"access_tier": "owner", "channel_id": "!r:x"}, "m")
        self.assertTrue(plain.split("\n")[0].endswith("task_id=t"))

    def test_result_body_label(self):
        out = rl.Outcome("ok", url="u")
        self.assertEqual(rl.result_body(out, "no-send"), "[no-send]\n# claude routine session: u\n")
        self.assertEqual(rl.result_body(out, "no-send", label="x"), "[no-send]\n# x: u\n")


class DelegationTest(unittest.TestCase):
    def test_both_adapters_delegate_to_the_shared_loop(self):
        shared = ("pending", "serve", "read_task", "admit", "schedule_retry")
        for mod, inherited in ((ra, shared + ("finish", "next_ready")), (sa, shared)):
            self.assertTrue(issubclass(mod.Adapter, rl.RelayLoop), mod)
            for name in inherited:
                self.assertIs(getattr(mod.Adapter, name), getattr(rl.RelayLoop, name), (mod, name))
            for name in ("build_payload", "result_body", "Outcome", "log"):
                self.assertIs(getattr(mod, name), getattr(rl, name), (mod, name))
            for name in ("acquire_lock", "non_owner_note", "write_atomic"):
                self.assertIs(getattr(mod, name), getattr(task_io, name), (mod, name))
        self.assertIs(ra.RateLimiter, rl.RateLimiter)
        for name in ("parse_task", "load_done", "record_done", "write_atomic", "non_owner_note"):
            self.assertIs(getattr(rl, name), getattr(task_io, name), name)
        for name in ("parse_task", "write_atomic", "load_done", "record_done", "acquire_lock", "pid_alive",
                     "non_owner_note", "attested_headers", "NON_OWNER_NOTE"):
            self.assertIs(getattr(cc, name), getattr(task_io, name), name)
        self.assertIs(task_fields.attested_headers, task_io.attested_headers)


if __name__ == "__main__":
    unittest.main()

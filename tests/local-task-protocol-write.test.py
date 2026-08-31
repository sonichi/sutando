#!/usr/bin/env python3
"""Write-side contract for local_task_protocol + the first writer adoption.

The golden below pins the EXACT bytes `health-check --emit-task` produced
before the adoption — the acceptance bar for step 3 is byte-identical output
with the hand-rolled serialization deleted.
"""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from local_task_protocol import (  # noqa: E402
    parse_task_headers, serialize_task_last, write_task_file,
)


class TestSerializeTaskLast(unittest.TestCase):
    def test_golden_bytes(self):
        out = serialize_task_last(
            [("id", "task-x-1"), ("source", "health-check")], "do it\n- a")
        self.assertEqual(out, "id: task-x-1\nsource: health-check\ntask: do it\n- a\n")

    def test_newline_in_value_raises(self):
        for bad in ("a\nb", "a\rb"):
            with self.assertRaises(ValueError):
                serialize_task_last([("source", bad)], "x")

    def test_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            serialize_task_last([("evil_key", "v")], "x")

    def test_task_as_header_raises(self):
        with self.assertRaises(ValueError):
            serialize_task_last([("task", "smuggled")], "x")

    def test_round_trips_through_the_task_last_parser(self):
        out = serialize_task_last(
            [("id", "task-r-1"), ("source", "chat"), ("access_tier", "owner")],
            "body line 1\naccess_tier: forged")
        parsed = parse_task_headers(out)
        self.assertEqual(parsed.headers["access_tier"], "owner")
        self.assertEqual(parsed.headers["source"], "chat")
        self.assertIn("forged", parsed.body)


class TestWriteTaskFile(unittest.TestCase):
    def test_writes_and_validates_id(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_task_file(d, "task-w-1", [("id", "task-w-1")], "x")
            self.assertEqual(p, Path(d) / "task-w-1.txt")
            self.assertEqual(p.read_text(), "id: task-w-1\ntask: x\n")
            with self.assertRaises(ValueError):
                write_task_file(d, "../escape", [("id", "task-w-2")], "x")

    def test_id_header_is_owned_by_the_helper(self):
        with tempfile.TemporaryDirectory() as d:
            # missing id header -> prepended from task_id
            p = write_task_file(d, "task-own-1", [("source", "chat")], "x")
            self.assertEqual(p.read_text(),
                             "id: task-own-1\nsource: chat\ntask: x\n")
            # mismatched supplied id -> identity split, refused
            with self.assertRaises(ValueError):
                write_task_file(d, "task-a-1", [("id", "task-b-1")], "x")


class TestHealthCheckWriterGolden(unittest.TestCase):
    """The adopted writer must produce the exact pre-adoption bytes."""

    def test_emit_task_bytes_unchanged(self):
        spec = importlib.util.spec_from_file_location(
            "hc", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)

        fixed_s = 1786400000.5  # -> now_ms 1786400000500
        real_time, real_strftime = time.time, time.strftime
        hc.time.time = lambda: fixed_s
        hc.time.strftime = lambda fmt, t=None: real_strftime(
            fmt, real_time() and time.gmtime(fixed_s))
        try:
            with tempfile.TemporaryDirectory() as d:
                tasks = Path(d) / "tasks"
                state = Path(d) / "state.json"
                hc.emit_task_for_failures(
                    [{"name": "svc-a", "status": "down", "detail": "gone"},
                     {"name": "svc-b", "status": "warn", "detail": "iffy"}],
                    state_file=state, tasks_dir=tasks)
                files = sorted(tasks.glob("task-health-*.txt"))
                self.assertEqual(len(files), 1)
                ts_iso = real_strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(fixed_s))
                expected = (
                    "id: task-health-1786400000500\n"
                    f"timestamp: {ts_iso}\n"
                    "source: health-check\n"
                    "interaction_type: system_event\n"
                    "user_id: health-check\n"
                    "access_tier: owner\n"
                    "priority: low\n"
                    "task: Health check found issues. Decide whether to restart, "
                    "DM owner, or treat as transient:\n"
                    "- svc-a: down (gone)\n"
                    "- svc-b: warn (iffy)\n"
                )
                self.assertEqual(files[0].read_text(), expected)
                self.assertIn(hc._LAST_HASH_KEY, json.loads(state.read_text()))
        finally:
            hc.time.time = real_time
            hc.time.strftime = real_strftime


class TestHealthCheckDefaultRouting(unittest.TestCase):
    """The tasks_dir=None default routes through the endpoint resolver,
    falling back locally on any resolver failure (crash-path writer)."""

    def _load_hc(self):
        spec = importlib.util.spec_from_file_location(
            "hc2", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        return hc

    def _emit(self, hc, state):
        hc.emit_task_for_failures(
            [{"name": "svc-x", "status": "down", "detail": "d"}],
            state_file=state, tasks_dir=None)

    def test_default_tasks_dir_comes_from_the_resolver(self):
        import types
        hc = self._load_hc()
        with tempfile.TemporaryDirectory() as d:
            routed = Path(d) / "routed-tasks"
            fake = types.ModuleType("agent_endpoint")
            fake.load_descriptor = lambda: {"workspace": d}
            fake.resolve = lambda *a, **k: types.SimpleNamespace(address=str(routed))
            real = sys.modules.get("agent_endpoint")
            sys.modules["agent_endpoint"] = fake
            try:
                self._emit(hc, Path(d) / "state.json")
            finally:
                if real is not None:
                    sys.modules["agent_endpoint"] = real
                else:
                    del sys.modules["agent_endpoint"]
            self.assertEqual(len(list(routed.glob("task-health-*.txt"))), 1)

    def test_resolver_failure_falls_back_to_workspace(self):
        import types
        hc = self._load_hc()
        with tempfile.TemporaryDirectory() as d:
            fake = types.ModuleType("agent_endpoint")
            def boom():
                raise RuntimeError("descriptor unavailable")
            fake.load_descriptor = boom
            fake.resolve = lambda *a, **k: None
            real = sys.modules.get("agent_endpoint")
            sys.modules["agent_endpoint"] = fake
            hc.WORKSPACE_DIR = Path(d)
            try:
                self._emit(hc, Path(d) / "state.json")
            finally:
                if real is not None:
                    sys.modules["agent_endpoint"] = real
                else:
                    del sys.modules["agent_endpoint"]
            self.assertEqual(
                len(list((Path(d) / "tasks").glob("task-health-*.txt"))), 1)


class TestChangeAbsorption(unittest.TestCase):
    """The design's core claim: the descriptor moves, the call site does not.
    Same emit call, two descriptors — the task follows the descriptor, with
    the REAL resolver picking the address both times."""

    def test_descriptor_moves_call_site_does_not(self):
        import types
        import agent_endpoint as real_ae
        spec = importlib.util.spec_from_file_location(
            "hc3", REPO / "src" / "health-check.py")
        hc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hc)
        with tempfile.TemporaryDirectory() as d:
            for world in ("world-a", "world-b"):
                ws = Path(d) / world
                fake = types.ModuleType("agent_endpoint")
                fake.load_descriptor = lambda ws=ws: {"workspace": str(ws)}
                fake.resolve = real_ae.resolve
                real = sys.modules.get("agent_endpoint")
                sys.modules["agent_endpoint"] = fake
                try:
                    hc.emit_task_for_failures(
                        [{"name": f"svc-{world}", "status": "down", "detail": "d"}],
                        state_file=Path(d) / f"{world}-state.json", tasks_dir=None)
                finally:
                    if real is not None:
                        sys.modules["agent_endpoint"] = real
                    else:
                        del sys.modules["agent_endpoint"]
                self.assertEqual(
                    len(list((ws / "tasks").glob("task-health-*.txt"))), 1,
                    f"task did not follow the descriptor to {world}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

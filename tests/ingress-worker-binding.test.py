#!/usr/bin/env python3
"""Ingress binds a task to a pinned worker — the routing decision moved off the
lead and onto the one process that must be up for a task to exist at all.

The safety property is the negative one: a pin must never strand work. A dead
pinned worker, a missing table and a corrupt table all fall through to the
unassigned name, which anyone can claim.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pool_affinity as pa  # noqa: E402

ROOM = "!room:ag2.space"


def _state(td, *, pinned_to=None, alive=None, pinned_flag=True, raw=None):
    state = Path(td) / "state"
    (state / "cores").mkdir(parents=True)
    (state / "pool").mkdir()
    if raw is not None:
        (state / "pool" / "affinity.json").write_text(raw, encoding="utf-8")
    elif pinned_to:
        entry = {"instance": pinned_to, "ts": time.time()}
        if pinned_flag:
            entry["pinned"] = True
        (state / "pool" / "affinity.json").write_text(
            json.dumps({ROOM: entry}), encoding="utf-8")
    if alive:
        (state / "cores" / f"{alive}.alive").write_text("x", encoding="utf-8")
    return state


class RouteTo(unittest.TestCase):
    def test_pinned_and_alive_routes_to_that_worker(self):
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive="core-2")
            self.assertEqual(pa.route_to(s, ROOM), "core-2")

    def test_a_DEAD_pinned_worker_is_not_routed_to(self):
        """The stranding case: naming a silent worker is worse than scatter."""
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive=None)
            self.assertIsNone(pa.route_to(s, ROOM))

    def test_a_stale_beat_counts_as_dead(self):
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive="core-2")
            beat = s / "cores" / "core-2.alive"
            old = time.time() - (pa.ALIVE_WINDOW_S + 30)
            import os
            os.utime(beat, (old, old))
            self.assertIsNone(pa.route_to(s, ROOM))

    def test_a_future_dated_beat_counts_as_dead(self):
        """Clock skew must degrade to unassigned, never pin to a silent worker."""
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive="core-2")
            beat = s / "cores" / "core-2.alive"
            import os
            future = time.time() + 3600
            os.utime(beat, (future, future))
            self.assertIsNone(pa.route_to(s, ROOM))

    def test_a_sticky_entry_without_pinned_true_does_not_route(self):
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive="core-2", pinned_flag=False)
            self.assertIsNone(pa.route_to(s, ROOM))

    def test_unpinned_channel_and_no_channel_route_nowhere(self):
        with TemporaryDirectory() as td:
            s = _state(td, pinned_to="core-2", alive="core-2")
            self.assertIsNone(pa.route_to(s, "!other:ag2.space"))
            self.assertIsNone(pa.route_to(s, None))
            self.assertIsNone(pa.route_to(s, ""))

    def test_missing_and_corrupt_tables_degrade_rather_than_raise(self):
        with TemporaryDirectory() as td:
            s = _state(td, alive="core-2")
            self.assertEqual(pa.read_bindings(s), {})
            self.assertIsNone(pa.route_to(s, ROOM))
        with TemporaryDirectory() as td:
            s = _state(td, raw="{not json", alive="core-2")
            self.assertEqual(pa.read_bindings(s), {})
        with TemporaryDirectory() as td:
            s = _state(td, raw='["a list, not a table"]', alive="core-2")
            self.assertEqual(pa.read_bindings(s), {})

    def test_instance_alive_rejects_an_empty_name(self):
        with TemporaryDirectory() as td:
            self.assertFalse(pa.instance_alive(_state(td), ""))


class BoundDestIsExercised(unittest.TestCase):
    """Calls the SHIPPED function rather than grepping for it. A source scan
    passes while `_bound_dest` is called but never defined — which is exactly
    the state this file's first draft shipped."""

    def _bridge(self, state):
        sys.path.insert(0, str(ROOT / "packages" / "ag2-sparrow"))
        from ag2_sparrow import remote_gateway_bridge as b
        b._STATE = Path(state)
        return b

    def test_names_the_worker_when_pinned_and_alive(self):
        with TemporaryDirectory() as td:
            b = self._bridge(_state(td, pinned_to="core-2", alive="core-2"))
            got = b._bound_dest(Path(td) / "task-abc.txt", "task-abc", ROOM)
            self.assertEqual(got.name, "task-abc.assigned-core-2.txt")

    def test_leaves_it_unassigned_when_the_worker_is_dead(self):
        with TemporaryDirectory() as td:
            b = self._bridge(_state(td, pinned_to="core-2", alive=None))
            got = b._bound_dest(Path(td) / "task-abc.txt", "task-abc", ROOM)
            self.assertEqual(got.name, "task-abc.txt")

    def test_a_raising_policy_still_queues_the_task(self):
        """Ingress must never wedge on a routing read."""
        with TemporaryDirectory() as td:
            b = self._bridge(_state(td, pinned_to="core-2", alive="core-2"))
            # b.pool_affinity, not the flat import: they are different module
            # objects and patching the wrong one tests nothing.
            real = b.pool_affinity.route_to
            b.pool_affinity.route_to = lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("table on fire"))
            try:
                got = b._bound_dest(Path(td) / "task-abc.txt", "task-abc", ROOM)
            finally:
                b.pool_affinity.route_to = real
            self.assertEqual(got.name, "task-abc.txt")


class BridgeDelegates(unittest.TestCase):
    """The bridge must not re-derive the rule; two readers of one table that
    disagree is how the pin and the claim path stop agreeing on who owns a room."""

    def _bridge_src(self) -> str:
        return (ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow"
                / "remote_gateway_bridge.py").read_text(encoding="utf-8")

    def test_bridge_calls_the_shared_policy(self):
        src = self._bridge_src()
        self.assertIn("pool_affinity.route_to(", src)
        self.assertIn("from . import pool_affinity", src)

    def test_bridge_does_not_read_the_table_itself(self):
        src = self._bridge_src()
        self.assertNotIn("affinity.json", src)
        self.assertNotIn('"pinned"', src)

    def test_vendored_copy_matches_src(self):
        a = (ROOT / "src" / "pool_affinity.py").read_text(encoding="utf-8")
        b = (ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow"
             / "pool_affinity.py").read_text(encoding="utf-8")
        self.assertIn(a.strip(), b, "run tools/sync_from_src.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)

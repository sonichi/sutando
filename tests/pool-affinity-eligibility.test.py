#!/usr/bin/env python3
"""Eligibility from the pin table: which worker, if any, a room is bound to.

The safety property is the negative one — a pin must never strand work. A dead
pinned worker, a missing table and a corrupt table all answer "nobody", which
leaves the task claimable by the core.
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

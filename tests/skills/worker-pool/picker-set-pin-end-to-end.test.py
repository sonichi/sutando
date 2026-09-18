#!/usr/bin/env python3
"""The owner pins a room to a SET, from the task file to the delivered sentinel.

Every hop is the shipped one: the watcher's handler reads a real stamped task
file, the picker authorises it, the roster's own writer binds and publishes, and
a later task from that room is routed by the handler the watcher invokes. A unit
test on any one of those hops passed while the whole path could not carry a set
at all, because the only production `bind_room` caller refused two names.

Run: python3 tests/skills/worker-pool/picker-set-pin-end-to-end.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(1, str(REPO / "src"))

import task_envelope as te  # noqa: E402

import pool_advertise as pa  # noqa: E402

import pool_roster as pr  # noqa: E402

import pool_route_handler as prh  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
ROOM = "!abc:ag2.space"


class SetPinEndToEnd(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "tasks").mkdir(parents=True)
        (self.ws / "results").mkdir(parents=True)
        pr.compile_roster(self.ws, {W1: {"label": "one", "state": "live"},
                                    W2: {"label": "two", "state": "live"}}, {})

    def _task(self, tid, body, above=None, **below):
        """A task file in the gateway's shape: the tier lives below `task:`, so
        only the envelope stamp admits it — which is what authorises the pin.
        `above` is the region a body may never forge, `requested_worker` included."""
        text = f"id: {tid}\nreceiving_instance: @me:ag2.space\n"
        text += "".join(f"{k}: {v}\n" for k, v in (above or {}).items())
        text += f"task: {body}\n"
        text += "".join(f"{k}: {v}\n" for k, v in below.items())
        p = self.ws / "tasks" / f"{tid}.txt"
        p.write_text(te.stamp_text(text, self.ws))
        return p

    def _pin(self, tid="task-pin-set"):
        return self._task(
            tid,
            f"Pin room {ROOM} to workers {W2} {W1} — bound set, "
            "pool-restriction routing (worker picker)",
            source="ag2space", wire_source="worker-picker",
            channel_id=ROOM, user_id="@q:b", access_tier="owner")

    def _run(self, path, *extra):
        return prh.guarded_main(["--task-file", str(path), "--workspace", str(self.ws),
                                 "--results-dir", str(self.ws / "results"), *extra])

    def test_a_pinned_set_binds_publishes_and_then_routes(self):
        # 1. the task file -> the binding, applied on the handler's probe.
        self.assertEqual(self._run(self._pin(), "--probe"), prh.DECLINE,
                         "a picker command is the core's, never a bound worker's")
        self.assertEqual(pr.load_bindings(self.ws), {ROOM: [W2, W1]})

        # 2. the binding -> the compiled roster, in the addressed-set shape.
        roster = json.loads(pr.roster_path(self.ws).read_text(encoding="utf-8"))
        self.assertEqual(pr.members_of(roster["bindings"][ROOM]), [W2, W1])
        self.assertNotIsInstance(roster["bindings"][ROOM], list)

        # 3. the roster -> the advertisement, without waiting for another task.
        ad = json.loads(pa.advertisement_path(self.ws).read_text(encoding="utf-8"))
        row = ad["workers"]["bindings"][ROOM]
        self.assertEqual((row["instance"], row["instances"]), (W2, [W2, W1]))
        self.assertEqual(ad["workers"]["roster_version"], roster["version"])

        # 4. the roster -> routing. Unaddressed goes to the primary ALONE.
        work = self._task("task-work", "do the thing", source="ag2space", channel_id=ROOM)
        self.assertEqual(self._run(work), 0)
        self.assertTrue((self.ws / "deliveries" / W2 / "task-work.txt").exists())
        self.assertFalse((self.ws / "deliveries" / W1).exists(),
                         "the second member received a copy of one task")

        # 5. ... and an addressed member is the only recipient of its own task.
        addressed = self._task("task-addressed", "do the other thing",
                               above={"requested_worker": W1},
                               source="ag2space", channel_id=ROOM)
        self.assertEqual(self._run(addressed), 0)
        self.assertTrue((self.ws / "deliveries" / W1 / "task-addressed.txt").exists())
        self.assertFalse((self.ws / "deliveries" / W2 / "task-addressed.txt").exists())

    def test_a_pin_the_owner_did_not_send_changes_nothing(self):
        """The control for step 1: the same sentence at a non-owner tier leaves
        the binding, the roster version and the advertisement untouched."""
        before = (pr.load_bindings(self.ws),
                  json.loads(pr.roster_path(self.ws).read_text())["version"],
                  pa.advertisement_path(self.ws).read_bytes())
        p = self._task("task-pin-team",
                       f"Pin room {ROOM} to workers {W2} {W1} — bound set, "
                       "pool-restriction routing (worker picker)",
                       source="ag2space", wire_source="worker-picker",
                       channel_id=ROOM, access_tier="team")
        self._run(p, "--probe")
        self.assertEqual((pr.load_bindings(self.ws),
                          json.loads(pr.roster_path(self.ws).read_text())["version"],
                          pa.advertisement_path(self.ws).read_bytes()), before)


if __name__ == "__main__":
    unittest.main(verbosity=0)

#!/usr/bin/env python3
"""A roster write publishes: the advertisement follows every compile.

The pin defect this pins: two entry points existed, `bind_room` (roster only)
and `apply` (roster + advertisement), and a direct `bind_room` left the broker
routing on the old pin until the next task happened to re-publish. Now the
compile the mutators end in writes the advertisement, so a direct call is
harmless by construction, and a publish failure is raised as `PublishError`
(roster written, picker behind) rather than lost.

Run: python3 tests/skills/worker-pool/roster-write-publishes-advertisement.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[3] / "skills/worker-pool/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(1, str(Path(__file__).resolve().parents[3] / "src"))
import pool_advertise as pa  # noqa: E402
import pool_roster as pr  # noqa: E402

import worker_picker_commands as wpc  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
ROOM = "!pin:example.test"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "results").mkdir()
        pr.compile_roster(self.ws, {W1: {"state": "live", "label": "one"}}, {})

    def ad_path(self):
        return pa.advertisement_path(self.ws)

    def published(self):
        return pa._projection(json.loads(self.ad_path().read_text()))

    def wanted(self):
        ad = pa.advertisement(self.ws, now=0)
        return pa._projection({"report": ad["report"], "workers": ad["workers_snapshot"],
                               "profile_workers": ad["profile_patch"]["workers"]})

    def assertPublished(self):
        self.assertTrue(self.ad_path().exists(), "no advertisement after a roster write")
        self.assertEqual(self.published(), self.wanted())
        roster = json.loads(pr.roster_path(self.ws).read_text())
        doc = json.loads(self.ad_path().read_text())
        self.assertEqual(doc["workers"]["roster_version"], roster["version"])


class TestEveryMutatorPublishes(Base):
    def test_compile_roster_publishes(self):
        self.assertPublished()

    def test_direct_bind_room_publishes(self):
        self.ad_path().unlink()
        pr.bind_room(self.ws, ROOM, W1)
        self.assertPublished()
        self.assertEqual(self.published()["workers"]["bindings"][ROOM]["instance"], W1)

    def test_direct_unbind_room_publishes(self):
        pr.bind_room(self.ws, ROOM, W1)
        self.ad_path().unlink()
        pr.unbind_room(self.ws, ROOM)
        self.assertPublished()
        self.assertNotIn(ROOM, self.published()["workers"]["bindings"])

    def test_register_worker_publishes(self):
        self.ad_path().unlink()
        pr.register_worker(self.ws, W2, "two", ROOM)
        self.assertPublished()
        self.assertIn(W2, self.published()["profile_workers"])

    def test_ensure_advertisement_has_nothing_to_repair_after_a_direct_pin(self):
        pr.bind_room(self.ws, ROOM, W1)
        before = self.ad_path().stat().st_mtime_ns
        self.assertEqual(pa.ensure_advertisement(self.ws), self.ad_path())
        self.assertEqual(self.ad_path().stat().st_mtime_ns, before, "ensure rewrote: the pin had not published")


class TestPublishFailureIsNamed(Base):
    def test_a_failed_publish_raises_after_the_roster_is_written(self):
        real = pa.write_advertisement
        pa.write_advertisement = lambda ws, now=None: (_ for _ in ()).throw(OSError(28, "No space left"))
        self.addCleanup(lambda: setattr(pa, "write_advertisement", real))
        with self.assertRaises(pr.PublishError) as cm:
            pr.bind_room(self.ws, ROOM, W1)
        self.assertIsInstance(cm.exception, OSError)
        self.assertEqual(cm.exception.roster["bindings"][ROOM], W1)
        self.assertEqual(json.loads(pr.roster_path(self.ws).read_text())["bindings"][ROOM], W1)

    def test_apply_records_nothing_when_the_publish_fails(self):
        real = pa.write_advertisement
        pa.write_advertisement = lambda ws, now=None: (_ for _ in ()).throw(OSError(28, "No space left"))
        self.addCleanup(lambda: setattr(pa, "write_advertisement", real))
        cmd = {"action": "pin", "room": ROOM, "workers": [W1], "dedicated": False}
        with self.assertRaises(pr.PublishError):
            wpc.apply(self.ws, cmd, task_id="task-pin-1")
        self.assertFalse((self.ws / "state" / "picker-applied.json").exists()
                         and ROOM in (json.loads((self.ws / "state" / "picker-applied.json").read_text()).get("rooms") or {}),
                         "a pin whose publish failed must stay retryable")


class TestApplyReportsThePublishedFile(Base):
    def test_apply_returns_the_path_the_compile_published(self):
        cmd = {"action": "pin", "room": ROOM, "workers": [W1], "dedicated": False}
        out = wpc.apply(self.ws, cmd, task_id="task-pin-2")
        self.assertEqual(Path(out["advertisement"]), self.ad_path())
        self.assertEqual(self.published(), self.wanted())
        self.assertEqual(out["roster_version"], json.loads(self.ad_path().read_text())["workers"]["roster_version"])


class TestTheDiscriminatorHasTeeth(Base):
    def test_without_publication_the_file_goes_stale(self):
        real = pr._publish
        pr._publish = lambda ws, roster: None
        self.addCleanup(lambda: setattr(pr, "_publish", real))
        pr.bind_room(self.ws, ROOM, W1)
        self.assertNotEqual(self.published(), self.wanted(), "control: a non-publishing bind must read as stale")


if __name__ == "__main__":
    unittest.main(verbosity=1)

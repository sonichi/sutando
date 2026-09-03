#!/usr/bin/env python3
"""`source:` names the RECEIVING lane, never the wire's contract label.

Both homeservers' brokers send source="ag2space" (the contract family), so a
task's origin homeserver was undecidable from its header — prod and dev lanes
stamped identical sources (owner finding, 2026-08-31). The lane's CHANNEL_DIR
is the origin authority; a differing wire label survives as wire_source.
Exercises the SHIPPED _write_task, not a copy of its field loop.

Run: python3 tests/gateway-source-names-the-lane.test.py
Exit: 0 on pass, 1 on fail.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "ag2-sparrow"))


def _load(channel_dir, instance, tasks_dir):
    os.environ["REMOTE_TASK_CHANNEL_DIR"] = channel_dir
    os.environ["GATEWAY_INSTANCE"] = instance
    os.environ.setdefault("REMOTE_TASK_TOKEN", "t")
    import ag2_sparrow.remote_gateway_bridge as m
    m = importlib.reload(m)
    m.TASKS_DIR = Path(tasks_dir)
    return m


class SourceNamesLane(unittest.TestCase):
    def _write(self, channel_dir, instance, wire_source):
        td = tempfile.mkdtemp(prefix="srclane-")
        self.addCleanup(__import__("shutil").rmtree, td, True)
        m = _load(channel_dir, instance, td)
        tid = m._write_task({"id": "9f2b1c", "task": "probe",
                            "source": wire_source, "access_tier": "owner"})
        self.assertIsNotNone(tid)
        body = (Path(td) / f"{tid}.txt").read_text()
        return body

    def test_dev_lane_stamps_its_own_name_and_keeps_wire(self):
        body = self._write("dev-ag2space", "dev", "ag2space")
        self.assertIn("source: dev-ag2space\n", body)
        self.assertIn("wire_source: ag2space\n", body)
        # line-anchored: "source: ag2space" is a SUBSTRING of the wire_source line
        self.assertNotIn("\nsource: ag2space\n", "\n" + body)

    def test_prod_lane_byte_identical_no_wire_line(self):
        body = self._write("ag2space", "", "ag2space")
        self.assertIn("source: ag2space\n", body)
        self.assertNotIn("wire_source:", body)

    def test_missing_wire_source_still_stamps_lane(self):
        body = self._write("dev-ag2space", "dev", None)
        self.assertIn("source: dev-ag2space\n", body)
        self.assertNotIn("wire_source:", body)


if __name__ == "__main__":
    r = unittest.main(exit=False).result
    ok = r.wasSuccessful()
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

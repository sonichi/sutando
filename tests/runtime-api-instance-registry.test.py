#!/usr/bin/env python3
"""Tests for the Sutando Instance Manifest registry (M1).

Contract (taxonomy parts 4/5): the manifest is a small, versioned,
secret-free existence record with the Server as single writer — atomic
writes, 0600, installed_at survives rewrites, clean shutdown marks stopped,
a missing manifest never fails shutdown, and discovery (list) is file-based
so it answers with no daemon running. A crash leaves status "running"
behind BY DESIGN (manifest-running + dead socket = stale_or_crashed).

Run: python3 tests/runtime-api-instance-registry.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import instance_registry as reg  # noqa: E402


class InstanceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def test_write_and_list_roundtrip(self):
        p = reg.write_manifest("@qingyun-001:ag2.space",
                               workspace="/ws", endpoint="/run/rt.sock",
                               backend="tmux", owner="@qingyun:ag2.space")
        m = json.loads(p.read_text())
        self.assertEqual(m["schema_version"], 1)
        self.assertEqual(m["identity"]["agent_id"], "@qingyun-001:ag2.space")
        self.assertEqual(m["endpoint"], {"type": "unix", "path": "/run/rt.sock"})
        self.assertEqual(m["status"], "running")
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["identity"]["agent_id"], "@qingyun-001:ag2.space")

    def test_file_is_private_and_secret_free(self):
        p = reg.write_manifest("a1", workspace="/ws", endpoint="/s.sock")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        text = p.read_text().lower()
        for needle in ("token", "secret", "password", "key"):
            self.assertNotIn(needle, text)

    def test_installed_at_survives_rewrite_and_stop_marks_stopped(self):
        p = reg.write_manifest("a1")
        first = json.loads(p.read_text())["installed_at"]
        reg.write_manifest("a1", status="running")
        self.assertEqual(json.loads(p.read_text())["installed_at"], first)
        reg.mark_stopped("a1")
        m = json.loads(p.read_text())
        self.assertEqual(m["status"], "stopped")
        self.assertEqual(m["installed_at"], first)

    def test_mark_stopped_missing_manifest_is_noop(self):
        reg.mark_stopped("never-registered")  # must not raise

    def test_unreadable_manifest_listed_not_hidden(self):
        (Path(self.tmp.name) / "broken.json").write_text("{nope")
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["error"], "unreadable manifest")

    def test_desired_state_roundtrip_and_listing(self):
        reg.write_manifest("a1", endpoint="/s.sock")
        # no desired file -> no desired_state key
        self.assertNotIn("desired_state", reg.list_instances()[0])
        reg.write_desired_state("a1", "paused", reason="owner pause",
                                restore={"pending_tasks": True})
        d = reg.read_desired_state("a1")
        self.assertEqual(d["desired_state"], "paused")
        self.assertEqual(d["restore"], {"pending_tasks": True})
        listed = reg.list_instances()
        self.assertEqual(len(listed), 1)  # .desired.json is not an instance
        self.assertEqual(listed[0]["desired_state"], "paused")
        with self.assertRaises(ValueError):
            reg.write_desired_state("a1", "exploded")

    def test_manifest_carries_structured_launcher(self):
        reg.write_manifest("a1", launcher={"type": "command",
                                           "executable": "/x/bin/sutando",
                                           "args": ["serve"]})
        m = reg.list_instances()[0]
        self.assertEqual(m["launcher"]["args"], ["serve"])

    def test_agent_id_is_filename_sanitized(self):
        p = reg.write_manifest("../evil/../../id")
        self.assertEqual(p.parent, Path(self.tmp.name))
        self.assertNotIn("/", p.name.replace(".json", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

    def _touch_launcher(self, name="fake-launch"):
        import stat as _stat
        launcher = Path(self.tmp.name) / name
        # a launcher that just creates a marker and exits 0 (readiness is
        # injected, so the launcher body is irrelevant to attachability)
        launcher.write_text("#!/bin/sh\ntouch \"$SUTANDO_STARTED_MARKER\"\n")
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        return launcher

    def test_start_not_registered_and_no_launcher(self):
        self.assertFalse(reg.start_instance("ghost", _ready=lambda m: {"attachable": True})["ok"])
        reg.write_manifest("a1", endpoint=str(Path(self.tmp.name) / "run" / "rt.sock"))
        self.assertIn("launcher", reg.start_instance(
            "a1", _ready=lambda m: {"attachable": False, "stage": "server"})["error"])

    def test_start_waits_for_attachable_then_marks_running(self):
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        launcher = self._touch_launcher()
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        # readiness flips to attachable only after the launcher marker appears
        marker = Path(self.tmp.name) / "started.marker"
        os.environ["SUTANDO_STARTED_MARKER"] = str(marker)
        ready = lambda m: {"attachable": marker.exists()} if marker.exists() \
            else {"attachable": False, "stage": "core"}
        out = reg.start_instance("a1", wait_s=8, _ready=ready)
        os.environ.pop("SUTANDO_STARTED_MARKER", None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], "started")
        self.assertEqual(reg.read_desired_state("a1")["desired_state"], "running")

    def test_start_idempotent_when_already_attachable(self):
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": "/bin/sh",
                                     "args": [], "working_directory": self.tmp.name})
        out = reg.start_instance("a1", _ready=lambda m: {"attachable": True})
        self.assertEqual(out["state"], "already_running")

    def test_start_timeout_names_the_failing_stage(self):
        import stat as _stat
        sock = Path(self.tmp.name) / "run" / "rt.sock"
        # a launcher that stays alive so we reach the timeout (not exit) branch
        launcher = Path(self.tmp.name) / "sleeper"
        launcher.write_text("#!/bin/sh\nsleep 5\n")
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        reg.write_manifest("a1", endpoint=str(sock),
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        out = reg.start_instance(
            "a1", wait_s=1,
            _ready=lambda m: {"attachable": False, "stage": "core"})
        import subprocess
        subprocess.run(["pkill", "-f", str(launcher)], capture_output=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "core")
        self.assertIn("Core did not become attachable", out["error"])

    def test_start_injects_instance_env_from_manifest(self):
        # the launcher records its own env; assert instance vars came from the
        # manifest, not this test's shell
        sock = Path(self.tmp.name) / "run" / "q-1" / "rt.sock"
        envdump = Path(self.tmp.name) / "env.txt"
        import stat as _stat
        launcher = Path(self.tmp.name) / "dump-env"
        launcher.write_text("#!/bin/sh\nenv > \"%s\"\n" % envdump)
        launcher.chmod(launcher.stat().st_mode | _stat.S_IXUSR)
        reg.write_manifest("q-1", endpoint=str(sock), instance="q-1",
                           tmux_socket="/run/q-1/tmux.sock", session="core-q1",
                           config_dir="/cfg/q-1",
                           launcher={"type": "process", "executable": str(launcher),
                                     "args": [], "working_directory": self.tmp.name})
        # readiness true once the env dump exists
        reg.start_instance("q-1", wait_s=5,
                           _ready=lambda m: {"attachable": envdump.exists()})
        text = envdump.read_text()
        self.assertIn("SUTANDO_INSTANCE_ID=q-1", text)
        self.assertIn("SUTANDO_TMUX_SOCKET=/run/q-1/tmux.sock", text)
        self.assertIn("SUTANDO_TMUX_SESSION=core-q1", text)
        self.assertIn("CLAUDE_CONFIG_DIR=/cfg/q-1", text)

    def test_agent_id_is_filename_sanitized(self):
        p = reg.write_manifest("../evil/../../id")
        self.assertEqual(p.parent, Path(self.tmp.name))
        self.assertNotIn("/", p.name.replace(".json", ""))




class ResolveEitherIdTests(unittest.TestCase):
    """`sutando list` displays instance_id; attach/start must accept it.
    Regression for the 2026-08-08 UX finding: `attach default` -> not_registered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = reg.registry_dir
        reg.registry_dir = lambda: Path(self.tmp.name)

    def tearDown(self):
        reg.registry_dir = self._orig
        self.tmp.cleanup()

    def _write(self, agent_id, instance_id, tmux=True):
        m = {"schema_version": 1, "instance_id": instance_id,
             "identity": {"agent_id": agent_id},
             "runtime": ({"type": "tmux", "tmux_socket": "/tmp/t.sock",
                          "session": "s"} if tmux else {})}
        (Path(self.tmp.name) / (reg._SAFE_ID.sub("_", agent_id) + ".json")
         ).write_text(json.dumps(m))

    def test_exact_agent_id_wins(self):
        self._write("@a:x", "default")
        r = reg.resolve_agent_id("@a:x")
        self.assertTrue(r["ok"]); self.assertEqual(r["agent_id"], "@a:x")

    def test_instance_id_resolves_unique(self):
        self._write("@a:x", "default")
        r = reg.resolve_agent_id("default")
        self.assertTrue(r["ok"]); self.assertEqual(r["agent_id"], "@a:x")

    def test_unknown_is_not_registered(self):
        r = reg.resolve_agent_id("nope")
        self.assertFalse(r["ok"]); self.assertIn("not_registered", r["error"])

    def test_ambiguous_names_candidates_never_guesses(self):
        self._write("@a:x", "default"); self._write("@b:x", "default")
        r = reg.resolve_agent_id("default")
        self.assertFalse(r["ok"])
        self.assertIn("@a:x", r["error"]); self.assertIn("@b:x", r["error"])

    def test_attach_by_instance_id_end_to_end(self):
        self._write("@a:x", "default")
        r = reg.attach("default")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["argv"][:2], ["tmux", "-S"])

if __name__ == "__main__":
    unittest.main(verbosity=2)

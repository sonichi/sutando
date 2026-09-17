#!/usr/bin/env python3
"""Real-filesystem controls for the composite (agent_id, instance_id) key.

Two properties only a filesystem can falsify, so these tests create files,
make directories and BIND sockets instead of asserting about strings:

  * CASE — macOS (and Windows) ship case-insensitive volumes by default, where
    an encoding that passed ASCII case through let two accepted sibling
    instances silently become ONE manifest and ONE socket/lock directory. The
    probe records which world each run proved, and a positive control shows
    the volume really can express the collision.
  * LENGTH — an accepted 22-character instance id escaped past NAME_MAX and
    far past the AF_UNIX sun_path cap, surfacing as ENAMETOOLONG at manifest
    and run-dir creation; and the representative enrolled mxid overran the
    104-byte socket cap on a default macOS install.

Run: python3 tests/runtime-api-instance-key-fs.test.py
Exit: 0 on pass, 1 on fail.
"""
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import instance_key as km  # noqa: E402

import instance_registry as reg  # noqa: E402

import rundir  # noqa: E402

MXID = "@sutando-qingyun-001:ag2.space"
_ENV = ("SUTANDO_RUN_DIR", "SUTANDO_RUNTIME_SOCKET", "SUTANDO_RUNTIME_STATE",
        "SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID",
        "SUTANDO_INSTANCE_REGISTRY")


def case_insensitive(d: Path) -> bool:
    probe = d / "casEprobe"
    probe.write_text("x")
    hit = (d / "CASEPROBE").exists()
    probe.unlink()
    return hit


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.saved = {k: os.environ.pop(k, None) for k in _ENV}
        os.environ["SUTANDO_RUN_DIR"] = str(self.base / "run")
        os.environ["SUTANDO_RUNTIME_STATE"] = str(self.base / "state")
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = str(self.base / "instances")

    def tearDown(self):
        for k in _ENV:
            os.environ.pop(k, None)
            if self.saved.get(k) is not None:
                os.environ[k] = self.saved[k]
        self.tmp.cleanup()


class CaseCollisionTests(Base):
    def test_probe_reports_which_world_this_run_proved(self):
        print(f"\n  [fs] case_insensitive={case_insensitive(self.base)}")
        self.assertIn(case_insensitive(self.base), (True, False))

    def test_positive_control_raw_names_really_do_collide_here(self):
        """Without this, a green suite on a case-SENSITIVE volume would look
        identical to a real fix. Raw (unescaped) siblings must collide exactly
        where the encoded ones must not."""
        if not case_insensitive(self.base):
            self.skipTest("case-sensitive volume — collision is not expressible")
        (self.base / "agent+Blue").write_text("first")
        (self.base / "agent+blue").write_text("second")
        self.assertEqual((self.base / "agent+Blue").read_text(), "second")

    def test_sibling_instances_differing_only_in_case_stay_two_manifests(self):
        reg.write_manifest("agent", instance="Blue", endpoint="/one.sock")
        reg.write_manifest("agent", instance="blue", endpoint="/two.sock")
        rows = reg.list_instances()
        self.assertEqual(len(rows), 2, f"a tuple was overwritten: {rows}")
        self.assertEqual({r["endpoint"]["path"] for r in rows},
                         {"/one.sock", "/two.sock"})

    def test_sibling_instances_differing_only_in_case_get_two_run_dirs(self):
        up = rundir.instance_run_dir("Blue", agent="agent")
        low = rundir.instance_run_dir("blue", agent="agent")
        up.mkdir(parents=True)
        low.mkdir(parents=True)
        self.assertFalse(os.path.samefile(up, low),
                         f"two instances share one socket/lock dir: {up} {low}")
        self.assertNotEqual(rundir.socket_path("Blue", agent="agent"),
                            rundir.socket_path("blue", agent="agent"))

    def test_actors_differing_only_in_case_stay_distinct(self):
        self.assertNotEqual(km.instance_key("Agent"), km.instance_key("agent"))


class LengthBoundTests(Base):
    LONG = "日" * 22  # 22 accepted characters, 66 raw bytes, 198 escaped

    def test_key_is_byte_bounded(self):
        key = km.instance_key(self.LONG, self.LONG)
        self.assertLessEqual(len(key.encode()), km.MAX_KEY_BYTES)

    def test_bounded_keys_stay_distinct_per_tuple(self):
        self.assertNotEqual(km.instance_key(self.LONG, self.LONG + "a"),
                            km.instance_key(self.LONG, self.LONG + "b"))

    def test_bounded_key_is_not_claimed_reversible(self):
        with self.assertRaises(ValueError):
            km.decode_key(km.instance_key(self.LONG, self.LONG))

    def test_manifest_for_a_long_id_is_creatable(self):
        p = reg.write_manifest(self.LONG, instance=self.LONG, endpoint="/x.sock")
        self.assertTrue(p.exists())
        self.assertEqual(len(reg.list_instances()), 1)

    def test_run_dir_and_socket_for_a_long_id_are_creatable_and_bindable(self):
        d = rundir.instance_run_dir(self.LONG, agent=self.LONG)
        d.mkdir(parents=True)
        sock = rundir.socket_path(self.LONG, agent=self.LONG)
        self.assertLessEqual(len(sock.encode()), rundir.SUN_PATH_MAX)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(sock)  # the definitive control: ENAMETOOLONG raises here

    def test_representative_mxid_fits_the_socket_cap_on_the_real_run_dir(self):
        os.environ.pop("SUTANDO_RUN_DIR", None)
        sock = rundir.socket_path("default", agent=MXID)
        self.assertLessEqual(
            len(sock.encode()), rundir.SUN_PATH_MAX,
            f"the enrolled mxid overruns the AF_UNIX cap on the shipped run "
            f"dir: {len(sock.encode())}B {sock}")

    def test_socket_is_bindable_at_the_shipped_run_dir_length(self):
        """Same byte length as the default macOS run dir, but inside tmp so the
        test never writes to the real Application Support tree."""
        target = len(str(Path.home() / "Library" / "Application Support"
                         / "space.ag2.app" / "run").encode())
        root = self.base / "r"
        while len(str(root).encode()) < target:
            root = root / "p"
        root.mkdir(parents=True)
        os.environ["SUTANDO_RUN_DIR"] = str(root)
        sock = rundir.socket_path("default", agent=MXID)
        Path(sock).parent.mkdir(parents=True, exist_ok=True)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(sock)

    def test_an_unusable_run_dir_fails_loudly_not_as_enametoolong(self):
        root = self.base
        while len(str(root).encode()) < rundir.SUN_PATH_MAX - 10:
            root = root / "padpadpad"
        os.environ["SUTANDO_RUN_DIR"] = str(root)
        with self.assertRaises(ValueError) as cm:
            rundir.socket_path("default", agent=MXID)
        self.assertIn("SUTANDO_RUN_DIR", str(cm.exception))
        # The lock has no sun_path cap, and an explicit socket sidesteps the
        # one that does — neither may be blocked by the guard above.
        self.assertTrue(rundir.lock_path("default", agent=MXID))
        os.environ["SUTANDO_RUNTIME_SOCKET"] = "/tmp/explicit.sock"
        self.assertEqual(rundir.socket_path("default", agent=MXID),
                         "/tmp/explicit.sock")


if __name__ == "__main__":
    unittest.main(verbosity=2)

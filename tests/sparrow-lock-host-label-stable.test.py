#!/usr/bin/env python3
"""The poller lock's host label is fixed for the life of the process.

A lock acquired under one label (gethostname, before the monorepo src/ is importable) must
still be ours after util_paths becomes importable: heartbeat, retain and release compare the
recorded host against the label, so a label that changes mid-run reads our own lock as foreign.
Each case runs in a fresh interpreter so module state and sys.path start clean.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
SRC = REPO / "src"
LABEL = "Util-Paths-Label-Not-The-Hostname"


def _run(body: str, tmp: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": tmp,
        "AGENT_CONNECT_STATE_DIR": str(Path(tmp) / "state"),
        "AGENT_CONNECT_TASK_DIR": str(Path(tmp) / "tasks"),
        "AGENT_CONNECT_RESULT_DIR": str(Path(tmp) / "results"),
        "REMOTE_TASK_TOKEN": "http://127.0.0.1:9|dummy",
        "SUTANDO_HOST_LABEL": LABEL,
    }
    prelude = textwrap.dedent(f"""
        import json, os, sys
        sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != {str(SRC)!r}]
        sys.path.insert(0, {str(PKG)!r})
        SRC = {str(SRC)!r}
    """)
    out = subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(body)], env=env,
                         cwd=tmp, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


class HostLabelStableTest(unittest.TestCase):
    def test_lock_survives_util_paths_becoming_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("""
                from ag2_sparrow import workspace_lock as wl
                ws = os.environ["AGENT_CONNECT_STATE_DIR"] + "/.."
                acq = wl.acquire("role-x", ws).status
                before = json.load(open(ws + "/state/locks/role-x.lock"))["host"]
                sys.path.insert(0, SRC)
                import util_paths
                print(json.dumps({"acq": acq, "before": before,
                                  "util": util_paths._host_label(),
                                  "hb": wl.heartbeat("role-x", ws),
                                  "retain": wl.retain("role-x", ws),
                                  "after": wl._host_label()}))
                wl.release("role-x", ws)
            """, tmp)
            self.assertEqual(r["util"], LABEL)
            self.assertNotEqual(r["before"], LABEL)
            self.assertEqual(r["after"], r["before"])
            self.assertTrue(r["hb"])
            self.assertTrue(r["retain"])
            self.assertFalse((Path(tmp) / "state" / "locks" / "role-x.lock").exists())

    def test_bridge_profile_does_not_edit_sys_path_or_lose_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("""
                import ag2_sparrow.remote_gateway_bridge as rgb
                ok = rgb._acquire_singleton()
                path_before = list(sys.path)
                host_id = rgb._profile_host_id()
                print(json.dumps({"ok": ok, "hb": rgb._heartbeat_singleton(),
                                  "path_same": sys.path == path_before,
                                  "host_id": host_id, "lock": rgb._stable_host_label()}))
            """, tmp)
            self.assertTrue(r["ok"])
            self.assertTrue(r["path_same"])
            self.assertTrue(r["hb"])
            self.assertEqual(r["host_id"], r["lock"])

    def test_startup_resolution_gives_lock_and_profile_the_stable_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("""
                import ag2_sparrow.remote_gateway_bridge as rgb
                rgb._resolve_host_label()
                ok = rgb._acquire_singleton()
                lock = json.load(open(str(rgb._LOCK_WS) + "/state/locks/" + rgb._LOCK_ROLE + ".lock"))
                print(json.dumps({"ok": ok, "hb": rgb._heartbeat_singleton(),
                                  "host": lock["host"], "host_id": rgb._profile_host_id()}))
            """, tmp)
            self.assertTrue(r["ok"])
            self.assertTrue(r["hb"])
            self.assertEqual(r["host"], LABEL)
            self.assertEqual(r["host_id"], LABEL)

    def test_main_resolves_the_label_before_taking_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("""
                import ag2_sparrow.remote_gateway_bridge as rgb
                calls = []
                rgb._resolve_host_label = lambda: calls.append("resolve")
                def _acquire():
                    calls.append("acquire")
                    return False
                rgb._acquire_singleton = _acquire
                try:
                    rgb.main()
                    code = None
                except SystemExit as e:
                    code = e.code
                print(json.dumps({"calls": calls, "code": code}))
            """, tmp)
            self.assertEqual(r["code"], 75)
            self.assertEqual(r["calls"], ["resolve", "acquire"])

    def test_an_unreadable_host_does_not_stop_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = _run("""
                import socket
                import ag2_sparrow.remote_gateway_bridge as rgb
                sys.modules["util_paths"] = None
                def _no_host():
                    raise OSError("no host")
                socket.gethostname = _no_host
                rgb._resolve_host_label()
                print(json.dumps({"host_id": rgb._profile_host_id()}))
            """, tmp)
            self.assertEqual(r["host_id"], "unknown-host")


if __name__ == "__main__":
    unittest.main()

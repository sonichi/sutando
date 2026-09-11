#!/usr/bin/env python3
"""Kewei's #3303 production blockers, pinned: manifest launcher restores THIS
daemon, cross-instance start serves the TARGET identity, and a future-dated
heartbeat never renders healthy.

Run: python3 tests/runtime-api-restore-identity.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

# flake8: noqa: E402 — imports come after the sys.path bootstrap above
import agents_view as av
import identity_view as iv
import instance_registry as ir
import runtime_view as rv


class LauncherDefaultRestoresDaemon(unittest.TestCase):
    def test_default_launcher_is_the_daemon_front_door(self):
        # the manifest's launcher must be able to bring THIS daemon back:
        # startup.sh never reaches server.py, so it is not a valid default
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_LAUNCHER_EXECUTABLE",
                            "SUTANDO_LAUNCHER_ARGS")}
        with mock.patch.dict(os.environ, env, clear=True):
            import importlib
            import server as srv
            importlib.reload(srv)
            repo = Path(srv.__file__).resolve().parent.parent.parent
            exe = (os.environ.get("SUTANDO_LAUNCHER_EXECUTABLE")
                   or str(repo / "bin" / "sutando"))
            # derive exactly as _register_instance does: front door + serve
            self.assertTrue(exe.endswith("bin/sutando"))
            args = json.loads(
                os.environ.get("SUTANDO_LAUNCHER_ARGS") or '["serve"]')
            self.assertEqual(args, ["serve"])
            src = Path(srv.__file__).read_text()
            self.assertIn('repo / "bin" / "sutando"', src)
            self.assertNotIn('repo / "src" / "startup.sh"', src)


class CrossInstanceStartIdentity(unittest.TestCase):
    def test_child_env_carries_target_agent_id_not_callers(self):
        tmp = tempfile.TemporaryDirectory()
        reg = Path(tmp.name)
        sock = str(reg / "t.sock")
        stub = reg / "launcher.sh"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        manifest = {
            "agent_id": "@target:x", "instance_id": "target-instance",
            "identity": {"agent_id": "@target:x"},
            "endpoint": {"type": "unix_socket", "path": sock},
            "launcher": {"type": "process", "executable": str(stub),
                         "args": [], "working_directory": tmp.name},
            "status": "stopped",
        }
        (reg / "@target:x.json").write_text(json.dumps(manifest))
        captured = {}

        class FakeProc:
            pid = 4242

            def poll(self):
                return None

        def fake_popen(argv, env=None, **kw):
            captured["env"] = env
            return FakeProc()

        def ready_after_spawn(_m):
            # attachable only once the launcher was actually spawned — the
            # pre/post-lock idempotency probes must both say not-running
            return {"attachable": "env" in captured}

        with mock.patch.dict(os.environ,
                             {"SUTANDO_AGENT_ID": "@caller:x",
                              "SUTANDO_INSTANCE_REGISTRY": str(reg)}), \
             mock.patch("subprocess.Popen", fake_popen):
            out = ir.start_instance("@target:x", wait_s=5.0,
                                    _ready=ready_after_spawn)
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(captured["env"]["SUTANDO_AGENT_ID"], "@target:x")
        self.assertEqual(captured["env"]["SUTANDO_INSTANCE_ID"],
                         "target-instance")
        tmp.cleanup()


class FutureDatedHeartbeat(unittest.TestCase):
    def _future_beat(self, root: Path, name: str):
        cores = root / "cores"
        cores.mkdir(parents=True, exist_ok=True)
        f = cores / f"{name}.alive"
        f.write_text(json.dumps({"agent_id": name, "host": name}))
        future = time.time() + 3600
        os.utime(f, (future, future))
        return f

    def test_agents_view_not_alive(self):
        with tempfile.TemporaryDirectory() as td:
            self._future_beat(Path(td), "skewed")
            v = av.AgentsView(td)
            entry = v._entry(Path(td) / "cores" / "skewed.alive")
            self.assertFalse(entry["alive"])
            self.assertLess(entry["beatAgeS"], 0)

    def test_runtime_view_offline(self):
        with tempfile.TemporaryDirectory() as td:
            self._future_beat(Path(td), "skewed")
            v = rv.RuntimeView(td, host_label="skewed")
            self.assertEqual(v.health()["state"], "offline")

    def test_identity_view_not_alive(self):
        with tempfile.TemporaryDirectory() as td:
            self._future_beat(Path(td), "skewed")
            v = iv.IdentityView(td, "@me:x", host_label="skewed")
            self.assertFalse(v.status()["alive"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

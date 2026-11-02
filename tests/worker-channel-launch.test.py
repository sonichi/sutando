#!/usr/bin/env python3
"""Worker launch names and room routing, using only synthetic credentials."""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import importlib.util
import os
import tempfile
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from _helpers.three_connections import ThreeConnections, ROOMS, SERVERS, gateway, token
import channel_env_resolve
import channel_credentials
import connections


class WorkerChannelLaunchTests(unittest.TestCase):
    def setUp(self):
        self.three = ThreeConnections(self)

    def test_names_never_read_values_or_files(self):
        class NamesOnly(dict):
            def __getitem__(self, key):
                raise AssertionError("credential value read")

        env = NamesOnly(AG2_DEVICE_ENV=None, AG2_REMOTE_TOKEN_S_SPACE=None,
                        REMOTE_TASK_TOKEN=None, REMOTE_TASK_URL_S_SPACE=None,
                        REMOTE_TASK_CHANNEL_DIR_S_SPACE=None, UNRELATED_SECRET=None)
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("file read")):
            self.assertEqual(channel_env_resolve.launch_env_names(env), sorted(set(env) - {"UNRELATED_SECRET"}))

    def test_launchers_carry_names_and_redact_probe(self):
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                env = dict(os.environ, SUTANDO_PY=sys.executable,
                           AG2_DEVICE_ENV=str(self.three.root / "launcher.env"),
                           SUTANDO_INSTANCE_ID="test-worker", SUTANDO_TMUX_SESSION="test-worker",
                           SUTANDO_TASKS_DIR=str(self.three.workspace / "deliveries"),
                           SUTANDO_WORKSPACE_DIR=str(self.three.workspace),
                           SUTANDO_RESULTS_DIR=str(self.three.workspace / "results"),
                           SUTANDO_INBOX_RESOLVER="fixture-resolver",
                           SUTANDO_POOL_DELIVERY_SCRIPT="fixture-delivery")
                script = "launch-codex-worker-session.sh" if runtime == "codex" else "launch-worker-session.sh"
                result = subprocess.run(["bash", str(ROOT / "skills/worker-pool/scripts" / script), "--print-env"],
                                        env=env, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                for name in ("AG2_DEVICE_ENV", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN_S_SPACE",
                             "AG2_REMOTE_TOKEN_DEV_S_SPACE"):
                    self.assertIn(name + "=<redacted>", result.stdout)
                for server in SERVERS:
                    self.assertNotIn(token(server), result.stdout)

    def test_codex_tmux_receives_the_launch_environment(self):
        path = ROOT / "tests/skills/worker-pool/codex-worker-auto-reset-launcher.test.py"
        spec = importlib.util.spec_from_file_location("codex_launch_fixture", path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        with tempfile.TemporaryDirectory() as tmp:
            result, _, log, _, _ = fixture.CodexWorkerResetLauncherTests()._launch(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            for server, (instance, _, _) in SERVERS.items():
                self.assertIn("-e " + connections.token_ref_for(instance) + "=" + token(server), log.read_text())

    def test_forwarded_environment_routes_each_room_as_core(self):
        core = dict(os.environ)
        seat = {name: core[name] for name in channel_env_resolve.launch_env_names(core)}
        for server, room in ROOMS.items():
            with self.subTest(server=server), mock.patch.dict(os.environ, seat, clear=True):
                got = channel_credentials.room_env(("REMOTE_TASK_URL", "AG2_ROOM_COLLAB_URL"),
                                                  ("ROOM_COLLAB_TOKEN", "REMOTE_TASK_TOKEN"), room,
                                                  workspace=self.three.workspace)
                self.assertEqual(got["ROOM_COLLAB_TOKEN"], token(server))
                self.assertEqual(got["REMOTE_TASK_URL"], "")
                self.assertEqual(got["AG2_ROOM_COLLAB_URL"], "")
                self.assertEqual(got["REMOTE_TASK_TOKEN"], "")

    def test_launcher_file_pointer_reaches_room_resolver(self):
        path = self.three.root / "launcher.env"
        path.write_text("AG2_REMOTE_TOKEN_S_SPACE=" + token("s.space") + "\n")
        core = {"AG2_DEVICE_ENV": str(path), "CLAUDE_CONFIG_DIR": str(self.three.config),
                "REMOTE_TASK_TOKEN": token("ag2.space")}
        seat = {name: core[name] for name in channel_env_resolve.launch_env_names(core)}
        with mock.patch.dict(os.environ, seat, clear=True):
            got = channel_credentials.room_env(("REMOTE_TASK_URL",), ("ROOM_COLLAB_TOKEN",),
                                              ROOMS["s.space"], workspace=self.three.workspace)
        self.assertEqual(got["ROOM_COLLAB_TOKEN"], token("s.space"))

    def test_wrong_lane_token_is_refused(self):
        seat = {"AG2_REMOTE_TOKEN_S_SPACE": token("ag2.space")}
        with mock.patch.dict(os.environ, seat, clear=True), \
                mock.patch.object(connections, "channel_launch_keys", return_value={}), \
                mock.patch.object(connections, "_channel_dir_tokens", return_value=[]):
            row = connections.connection_for_room(ROOMS["s.space"], self.three.workspace)
            with self.assertRaises(connections.NoConnectionForRoom):
                connections.connection_credential(row, seat, vault={})

    def test_primary_only_environment_stays_primary(self):
        core = {"REMOTE_TASK_TOKEN": token("ag2.space"), "REMOTE_TASK_URL": gateway("ag2.space")}
        self.assertEqual({name: core[name] for name in channel_env_resolve.launch_env_names(core)}, core)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A worker seat resolves a room's lane credential the way the core does.

tmux hands a new session the server's environment plus the launcher's `-e` list, so the
device file and app-support pointers the core resolves a secondary lane with never reached a
seat: an s.space room was answered with the primary lane's token, and the service refused it.
The launcher now forwards the pointers the core's resolver names (`channel_env_resolve
--lane-env`), never a token. Pinned end to end: the real launcher's forwarded env, then the
core's room router (`channel_credentials.room_env`) run under it in a child process.

Run: python3 tests/skills/worker-pool/worker-seat-lane-credential.test.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _helpers.hermetic_env import hermetic_env, isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[3]
CLAUDE_LAUNCHER = REPO / "skills/worker-pool/scripts/launch-worker-session.sh"
CODEX_LAUNCHER = REPO / "skills/worker-pool/scripts/launch-codex-worker-session.sh"

AG2 = "https://chat.ag2.space/relay"
S_SPACE = "https://chat.s.space/relay"
PRIMARY_TOKEN = AG2 + "|primary-secret"
LANE_TOKEN = S_SPACE + "|lane-secret"
TOKEN_NAMES = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN", "AG2_REMOTE_TOKEN_S_SPACE")

# The room client's variable names; the router blanks all but the first token one.
PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import connections
connections._vault_get = lambda vault, key: ""
from channel_credentials import room_env
out = {}
for room in sys.argv[3:]:
    try:
        env = room_env(("AG2_ROOM_COMMONS_URL", "REMOTE_TASK_URL"),
                       ("ROOM_COMMONS_TOKEN", "REMOTE_TASK_TOKEN"), room, workspace=sys.argv[2])
        out[room] = None if env is None else env["ROOM_COMMONS_TOKEN"]
    except connections.NoConnectionForRoom:
        out[room] = "refused"
print(json.dumps(out))
"""


class SeatLaneCredential(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "ws"
        (self.ws / "state").mkdir(parents=True)
        rows = [{"server": "ag2.space", "instance": "", "gateway_url": AG2, "token_ref": "REMOTE_TASK_TOKEN",
                 "primary": True},
                {"server": "s.space", "instance": "s-space", "gateway_url": S_SPACE,
                 "token_ref": "AG2_REMOTE_TOKEN_S_SPACE", "primary": False}]
        for r in rows:
            r.update(added_by="env", added_at="2026-10-01T00:00:00Z", verified=True, server_source="identity")
        (self.ws / "state" / "connections.json").write_text(
            json.dumps({"schema_version": 1, "connections": rows}))
        # The desktop layout: the lane's key lives in the launcher-named device file.
        self.app = self.tmp / "app"
        (self.app / "channels").mkdir(parents=True)
        (self.app / "channels" / "active-gateway-lane").write_text("s-space\n")
        self.device = self.app / "device.env"
        self.device.write_text(f'REMOTE_TASK_TOKEN="{PRIMARY_TOKEN}"\nAG2_REMOTE_TOKEN_S_SPACE="{LANE_TOKEN}"\n')
        self.device.chmod(0o600)
        self.ccd = self.tmp / "ccd"
        (self.ccd / "channels").mkdir(parents=True)
        # What the tmux server holds for every session: the primary token, never the lane's.
        self.server_env = {"CLAUDE_CONFIG_DIR": str(self.ccd), "REMOTE_TASK_TOKEN": PRIMARY_TOKEN,
                           "AG2_REMOTE_TOKEN": PRIMARY_TOKEN}
        self.core_env = {**self.server_env, "AG2_DEVICE_ENV": str(self.device),
                         "SUTANDO_APP_SUPPORT": str(self.app)}

    def forwarded(self, launcher, core_env):
        """The `-e` list the launcher hands tmux, as {name: value}."""
        env = hermetic_env(**core_env, SUTANDO_PY=sys.executable, SUTANDO_TMUX_SESSION="sutando-worker-t",
                           SUTANDO_INSTANCE_ID="t", SUTANDO_WORKSPACE_DIR=str(self.ws),
                           SUTANDO_TASKS_DIR=str(self.ws / "deliveries" / "t"),
                           SUTANDO_RESULTS_DIR=str(self.ws / "results"),
                           SUTANDO_INBOX_RESOLVER=str(self.tmp / "resolver"),
                           SUTANDO_POOL_DELIVERY_SCRIPT=str(self.tmp / "delivery.py"))
        r = subprocess.run(["bash", str(launcher), "--print-env"], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        pairs = [line.split("=", 1) for line in r.stdout.splitlines() if "=" in line and line != "-e"]
        return {k: v for k, v in pairs}

    def resolve(self, session_env, *rooms):
        env = hermetic_env(**session_env, SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")
        r = subprocess.run([sys.executable, "-c", PROBE, str(REPO / "src"), str(self.ws), *rooms],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_the_core_routes_each_room_to_its_own_lane(self):
        got = self.resolve(self.core_env, "!r:s.space", "!r:ag2.space")
        self.assertEqual(got, {"!r:s.space": LANE_TOKEN, "!r:ag2.space": PRIMARY_TOKEN})

    def test_a_seat_resolves_the_same_as_the_core(self):
        for launcher in (CLAUDE_LAUNCHER, CODEX_LAUNCHER):
            with self.subTest(launcher=launcher.name):
                fwd = self.forwarded(launcher, self.core_env)
                self.assertEqual(fwd.get("AG2_DEVICE_ENV"), str(self.device))
                self.assertEqual(fwd.get("SUTANDO_APP_SUPPORT"), str(self.app))
                got = self.resolve({**self.server_env, **fwd}, "!r:s.space", "!r:ag2.space")
                self.assertEqual(got, {"!r:s.space": LANE_TOKEN, "!r:ag2.space": PRIMARY_TOKEN})

    def test_the_launcher_forwards_no_token(self):
        for launcher in (CLAUDE_LAUNCHER, CODEX_LAUNCHER):
            with self.subTest(launcher=launcher.name):
                fwd = self.forwarded(launcher, {**self.core_env, "AG2_REMOTE_TOKEN_S_SPACE": LANE_TOKEN})
                self.assertFalse(set(TOKEN_NAMES) & set(fwd), fwd)
                self.assertFalse([v for v in fwd.values() if "secret" in v], fwd)

    def test_without_the_pointers_an_s_space_room_is_refused_not_given_the_primary_token(self):
        got = self.resolve(self.server_env, "!r:s.space", "!r:ag2.space")
        self.assertEqual(got, {"!r:s.space": "refused", "!r:ag2.space": PRIMARY_TOKEN})

    def test_a_lane_token_is_never_paired_with_another_lanes_gateway(self):
        # The device file's s.space key carries the primary lane's gateway: it is not the lane's.
        self.device.write_text(f'REMOTE_TASK_TOKEN="{PRIMARY_TOKEN}"\nAG2_REMOTE_TOKEN_S_SPACE="{AG2}|crossed"\n')
        fwd = self.forwarded(CLAUDE_LAUNCHER, self.core_env)
        got = self.resolve({**self.server_env, **fwd}, "!r:s.space")
        self.assertEqual(got, {"!r:s.space": "refused"})

    def test_a_seat_of_an_install_without_lanes_gets_nothing_new(self):
        fwd = self.forwarded(CLAUDE_LAUNCHER, self.server_env)
        self.assertFalse({"AG2_DEVICE_ENV", "SUTANDO_APP_SUPPORT"} & set(fwd), fwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)

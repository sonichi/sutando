#!/usr/bin/env python3
"""A declared bridge's wake child acts as the agent and never reloads the host's gateway credential.

sparrowd's wrapper hands a declared bridge its own token and marks its environment
(gateway_credential_sources.credentials_injected). The bridge passes the mark, and the host's
CLAUDE_CONFIG_DIR, to the REMOTE_TASK_ON_TASK wake child, with the agent's own token and URL in its
environment (gateway_credential_sources.wake_environ) and every other gateway alias dropped. Every
host credential reader there (room ops' channel .env and vault tiers, scripts/channel-env.sh) goes
through channel_token, which answers '' in a marked process tree; the env tier still reads the token. The sentinel runs the real chain: the wrapper builds the
environment, the bridge module queues a task, _fire_on_task starts a real subprocess, and that
subprocess resolves room ops' gateway and runs channel-env.sh. The control runs the same chain
unmarked, as the host's own bridge, and must see the host's credential (so the sentinel can fail).
Run: python3 tests/declared-bridge-wake-child-credentials.test.py"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import channel_token  # noqa: E402

sys.path.insert(0, str(REPO / "skills" / "agent-room-ops"))
import _gateway as room_ops_gateway  # noqa: E402
import sparrow_agent_bridge as bridge  # noqa: E402

from ag2_sparrow import _dirs, gateway_credential_sources as _sources, local_task_protocol  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli  # noqa: E402

AGENT = "@probe.agent:ag2.space"
SEG = "probe.agent_ag2.space"
BRIDGE = "ag2_sparrow.remote_gateway_bridge"
AGENT_URL = "https://agent-gw.example/relay"
AGENT_SECRET = "agent-secret-0123456789"
AGENT_TOKEN = f"{AGENT_URL}|{AGENT_SECRET}"
HOST_CANARY = "host-canary-9f8e7d6c5b4a"
HOST_URL = "https://host-gw.example/relay"

# The wake child: records what it could load, as booleans and paths, never a value (the agent token by digest:
# the bridge drops any variable holding its token).
WAKE = r"""
import hashlib, json, os, subprocess, sys
cfg, repo, out = os.environ["WAKE_CFG"], os.environ["WAKE_REPO"], os.environ["WAKE_OUT"]
opened = []
sys.addaudithook(lambda ev, a: opened.append(os.fsdecode(a[0])) if ev == "open" and a
                 and isinstance(a[0], (str, bytes)) and os.fsdecode(a[0]).startswith(cfg) else None)
sys.path.insert(0, os.path.join(repo, "skills", "agent-room-ops"))
import _gateway
base, headers = _gateway.gateway()
auth = headers.get("Authorization", "")
ce = subprocess.run(["bash", os.path.join(repo, "scripts", "channel-env.sh"), "ag2space"],
                    capture_output=True, text=True)
rec = {"marked": bool(os.environ.get("GATEWAY_CREDENTIALS_INJECTED")),
       "config_dir_kept": os.environ.get("CLAUDE_CONFIG_DIR") == cfg,
       "host_token_loaded": os.environ["WAKE_CANARY"] in auth, "host_url_loaded": base == os.environ["WAKE_HOST_URL"],
       "any_token": bool(auth), "agent_token": hashlib.sha256(auth.encode()).hexdigest() == os.environ["WAKE_AGENT_BEARER_SHA256"],
       "agent_url": base == os.environ["WAKE_AGENT_URL"], "opened": opened, "channel_env_rc": ce.returncode,
       "channel_env_named_a_file": bool(ce.stdout.strip())}
with open(os.path.join(out, "wake.json"), "w") as f:
    json.dump(rec, f)
"""


class WakeChild(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws, self.out, self.cfg = self.tmp / "ws", self.tmp / "out", self.tmp / "cfg"
        self.out.mkdir()
        channel = self.cfg / "channels" / "ag2space"
        channel.mkdir(parents=True)
        self.dotenv = channel / ".env"
        self.dotenv.write_text(f"REMOTE_TASK_TOKEN={HOST_CANARY}\nREMOTE_TASK_URL={HOST_URL}\n", encoding="utf-8")
        (channel / "access.json").write_text("{}", encoding="utf-8")
        script = self.tmp / "wake.py"
        script.write_text(WAKE, encoding="utf-8")
        self.host = {"CLAUDE_CONFIG_DIR": str(self.cfg), "WAKE_CFG": str(self.cfg), "WAKE_REPO": str(REPO),
                     "WAKE_OUT": str(self.out), "WAKE_CANARY": HOST_CANARY, "WAKE_HOST_URL": HOST_URL,
                     "WAKE_AGENT_BEARER_SHA256": hashlib.sha256(f"Bearer {AGENT_SECRET}".encode()).hexdigest(), "WAKE_AGENT_URL": AGENT_URL, "GATEWAY_URL": HOST_URL,
                     "REMOTE_TASK_ON_TASK": f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"}
        saved = (sys.modules.pop(BRIDGE, None), local_task_protocol._TASK_STAMPER, _dirs._injected)
        self.addCleanup(self.restore, saved)

    @staticmethod
    def restore(saved):
        sys.modules.pop(BRIDGE, None)
        if saved[0] is not None:
            sys.modules[BRIDGE] = saved[0]
        local_task_protocol.set_task_stamper(saved[1])
        _dirs._injected = saved[2]

    def declare(self):
        root = self.ws / "agents" / SEG
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": AGENT, "TOKEN_VAULT_KEY": cli.vault_key(SEG)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        return cli.bridge_home(root) / cli.DECLARATION

    def queue_one_task(self):
        """Import the bridge module where it runs and queue one task; its wake child records into out/."""
        mod = importlib.import_module(BRIDGE)
        # The stamper keys off the real workspace; it is not what this sentinel measures.
        local_task_protocol.set_task_stamper(None)
        task = {"id": "task-1784700000001", "task": "wake", "source": "ag2space", "channel_id": "!room:x",
                "user_id": "@someone:x", "access_tier": "owner", "timestamp": "2026-10-24T00:00:00Z"}
        got = mod._write_task(task)
        self.assertIsNotNone(got)
        deadline = time.time() + 30
        while time.time() < deadline and not (self.out / "wake.json").exists():
            time.sleep(0.05)
        time.sleep(0.2)
        return json.loads((self.out / "wake.json").read_text())

    def test_the_declared_bridges_wake_child_reads_the_room_as_the_agent_never_as_the_host(self):
        decl = self.declare()
        seen = {}

        def child(module, run_name, alter_sys):
            self.assertEqual(module, BRIDGE)
            seen["wake"] = self.queue_one_task()
        with mock.patch.dict(os.environ, self.host):
            rc = bridge.main([str(decl)], workspace=self.ws, get_key=lambda k: AGENT_TOKEN, run=child)
        self.assertEqual(rc, 0)
        wake = seen["wake"]
        self.assertTrue(wake["marked"], "the wake child inherits the mark")
        self.assertTrue(wake["config_dir_kept"], "the config root stays, for access.json")
        self.assertFalse(wake["host_token_loaded"], "room ops reloaded the host's token from its channel .env")
        self.assertFalse(wake["host_url_loaded"], "room ops reloaded the host's gateway URL")
        self.assertTrue(wake["agent_token"], "room ops in the wake child resolves exactly the agent's token")
        self.assertTrue(wake["agent_url"], "and the agent's gateway, not an inherited GATEWAY_URL")
        self.assertEqual(wake["opened"], [], "the wake child opened a file under the host's config root")
        self.assertNotEqual(wake["channel_env_rc"], 0)
        self.assertFalse(wake["channel_env_named_a_file"], "channel-env.sh handed the wake child a credential file")

    def test_control_the_hosts_own_bridge_wake_child_does_reload_it(self):
        # Unmarked, as the host's own bridge runs: the same sentinel sees the reload.
        env = dict(self.host, AGENT_CONNECT_TASK_DIR=str(self.tmp / "tasks"),
                   AGENT_CONNECT_RESULT_DIR=str(self.tmp / "results"), AGENT_CONNECT_STATE_DIR=str(self.tmp / "state"),
                   REMOTE_TASK_TOKEN=AGENT_TOKEN)
        with mock.patch.dict(os.environ, env):
            wake = self.queue_one_task()
        self.assertFalse(wake["marked"])
        self.assertTrue(wake["host_token_loaded"])
        self.assertTrue(wake["host_url_loaded"])
        self.assertIn(str(self.dotenv), wake["opened"])
        self.assertEqual(wake["channel_env_rc"], 0)
        self.assertTrue(wake["channel_env_named_a_file"])


class WakeEnviron(unittest.TestCase):
    """The owner's wake_environ: the agent's token and URL under the mark, the child untouched without it."""
    ALIASES = {"GATEWAY_TOKEN": "h1", "RELAY_TOKEN": "h2", "AG2_REMOTE_TOKEN": "h3", "REMOTE_MEDIA_HS_TOKEN": "h4",
               "GATEWAY_URL": "https://host/1", "RELAY_URL": "https://host/2", "AG2_REMOTE_URL": "https://host/3",
               "REMOTE_TASK_URL": "https://host/4", "KEEP_ME": "1"}

    def test_marked_child_carries_only_the_agents_token_and_url(self):
        child = dict(self.ALIASES, **{_sources.INJECTED_KEY: "1"})
        got = _sources.wake_environ(child, AGENT_TOKEN, AGENT_URL)
        self.assertEqual(got, {_sources.INJECTED_KEY: "1", "KEEP_ME": "1", "REMOTE_TASK_TOKEN": AGENT_TOKEN,
                               "REMOTE_TASK_URL": AGENT_URL})

    def test_marked_child_without_a_token_carries_none(self):
        got = _sources.wake_environ(dict(self.ALIASES, **{_sources.INJECTED_KEY: "1"}), "", AGENT_URL)
        self.assertEqual(got, {_sources.INJECTED_KEY: "1", "KEEP_ME": "1"})

    def test_unmarked_child_is_unchanged(self):
        self.assertEqual(_sources.wake_environ(self.ALIASES, AGENT_TOKEN, AGENT_URL), self.ALIASES)


class RoomOpsUnderTheMark(unittest.TestCase):
    """Room ops in a marked process: the env token or nothing, never the host's file or vault."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        channel = self.tmp / "channels" / "ag2space"
        channel.mkdir(parents=True)
        (channel / ".env").write_text(f"REMOTE_TASK_TOKEN={HOST_CANARY}\nREMOTE_TASK_URL={HOST_URL}\n", encoding="utf-8")
        self.marked = {_sources.INJECTED_KEY: "1", "CLAUDE_CONFIG_DIR": str(self.tmp)}

    def test_no_env_token_fails_closed(self):
        with mock.patch.dict(os.environ, self.marked):
            base, headers = room_ops_gateway.gateway()
        self.assertEqual((base, headers.get("Authorization")), ("", None))

    def test_the_env_token_is_the_one_used(self):
        with mock.patch.dict(os.environ, dict(self.marked, REMOTE_TASK_TOKEN=AGENT_TOKEN)):
            base, headers = room_ops_gateway.gateway()
        self.assertEqual((base, headers.get("Authorization")), (AGENT_URL, f"Bearer {AGENT_SECRET}"))


class ChannelTokenReaders(unittest.TestCase):
    """The owner's mark closes channel_token's file and vault readers: nothing is opened or looked up."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env_file = self.tmp / ".env"
        self.env_file.write_text(f"REMOTE_TASK_TOKEN={HOST_CANARY}\n", encoding="utf-8")

    def test_marked_readers_answer_nothing_without_reading(self):
        asked = []
        with mock.patch.dict(os.environ, _sources.bridge_token_environ("agent")):
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("opened")):
                self.assertEqual(channel_token.token_from_env_file("REMOTE_TASK_TOKEN", self.env_file), "")
            self.assertEqual(channel_token.token_from_vault("REMOTE_TASK_TOKEN", vault_get=asked.append), "")
        self.assertEqual(asked, [], "the vault was asked")

    def test_unmarked_readers_still_read(self):
        self.assertNotIn(_sources.INJECTED_KEY, os.environ)
        self.assertEqual(channel_token.token_from_env_file("REMOTE_TASK_TOKEN", self.env_file), HOST_CANARY)
        self.assertEqual(channel_token.token_from_vault("X", vault_get=lambda k: "v"), "v")


if __name__ == "__main__":
    unittest.main()

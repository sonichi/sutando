#!/usr/bin/env python3
"""A Discord bridge that keeps opening fresh gateway sessions is flagged, and says why it dropped.

The heartbeat stays fresh through such a loop, so health-check reads the session record the bridge
alone writes (src/gateway_sessions.py), never its log, which also carries message bodies. The bridge
logs discord.py's close and timeout records, and no raw gateway payload, to any handler.

Drives the production paths: the real bridge module's startup and on_ready, and run_all_checks().
Run: python3 tests/discord-gateway-sessions.test.py
"""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import os
import tempfile
from pathlib import Path

# The bridge resolves channel config at import; seed an empty allowlist before anything reads it.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-gateway-sessions-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')
os.environ["HOME"] = os.environ["CLAUDE_CONFIG_DIR"]
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")

import asyncio
import importlib.util
import io
import json
import logging
import subprocess
import sys
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import gateway_sessions  # noqa: E402

_hs = importlib.util.spec_from_file_location("hc_gateway_sessions", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_hs)
_hs.loader.exec_module(hc)

_bs = importlib.util.spec_from_file_location("bridge_gateway_sessions", REPO / "src" / "discord-bridge.py")
bridge = importlib.util.module_from_spec(_bs)
sys.modules[_bs.name] = bridge
_bs.loader.exec_module(bridge)
bridge.ACCESS_BACKUP_FILE = Path(tempfile.mkdtemp()) / "discord-access-backup.json"

NOW = 1_793_000_000.0
PAYLOAD = '{"content": "SECRET_BODY"}'


class SessionRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "state" / "discord-bridge.sessions.json"

    def write(self, body: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(body, encoding="utf-8")

    def test_sessions_in_the_last_hour_are_counted(self) -> None:
        for minutes in (90, 50, 30, 10, 1):
            gateway_sessions.record(self.path, NOW - minutes * 60)
        self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 4)

    def test_a_future_time_is_not_counted(self) -> None:
        self.write(json.dumps({"sessions": [NOW + 3600] * 9}))
        self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 0)

    def test_a_damaged_record_never_raises(self) -> None:
        for body in ("not json", "[]", '{"sessions": "x"}', '{"sessions": [true, "1", NaN, Infinity]}',
                     '{"sessions": [' + "1" * 400 + "]}"):
            with self.subTest(body=body[:40]):
                self.write(body)
                self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 0)
                gateway_sessions.record(self.path, NOW)
                self.assertEqual(json.loads(self.path.read_text())["sessions"], [NOW])

    def test_a_failed_publish_leaves_no_temp_file(self) -> None:
        with mock.patch.object(gateway_sessions.os, "replace", side_effect=OSError("disk full")):
            gateway_sessions.record(self.path, NOW)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), [])

    def test_the_record_is_bounded(self) -> None:
        for i in range(gateway_sessions.KEPT + 50):
            gateway_sessions.record(self.path, NOW + i * 0.01)
        self.assertEqual(len(json.loads(self.path.read_text())["sessions"]), gateway_sessions.KEPT)
        gateway_sessions.record(self.path, NOW + gateway_sessions.WINDOW_S + 10)
        self.assertEqual(len(json.loads(self.path.read_text())["sessions"]), 1)


class HealthRowTest(unittest.TestCase):
    """The discord-bridge row run_all_checks() produces, from the record alone."""

    def row(self, sessions: int, forged_log: bool = False) -> dict:
        with tempfile.TemporaryDirectory() as ws:
            state = Path(ws) / "state"
            state.mkdir()
            (state / "discord-bridge.heartbeat").write_text(str(int(time.time())))
            now = time.time()
            for i in range(sessions):
                gateway_sessions.record(state / "discord-bridge.sessions.json", now - 3000 + 60 * i)
            (Path(ws) / "logs").mkdir()
            log = "\n".join(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} Discord bridge ready: x (gateway session #{i})"
                            for i in range(9)) if forged_log else "ok\n"
            (Path(ws) / "logs" / "discord-bridge.log").write_text(log)
            original_run = subprocess.run

            def fake_run(cmd, *a, **k):
                if cmd[:3] == ["/bin/ps", "-o", "pid=,ppid="]:
                    return subprocess.CompletedProcess(cmd, 0, "4242 1\n", "")
                return original_run(cmd, *a, **k)

            with mock.patch.object(hc, "WORKSPACE_DIR", Path(ws)), \
                 mock.patch.object(hc, "_should_skip_bridge", side_effect=lambda ch, _e: ch != "discord"), \
                 mock.patch.object(hc, "probe_pids", return_value=(["4242"], True)), \
                 mock.patch.object(subprocess, "run", side_effect=fake_run):
                checks = hc.run_all_checks()
        return next(c for c in checks if c["name"] == "discord-bridge")

    def test_six_new_sessions_in_an_hour_warn(self) -> None:
        row = self.row(6)
        self.assertEqual(row["status"], "warn", row)
        self.assertIn("gateway flapping (6 new sessions", row["detail"])

    def test_five_do_not(self) -> None:
        self.assertNotIn("flapping", self.row(5)["detail"])

    def test_forged_log_lines_count_nothing(self) -> None:
        self.assertNotIn("flapping", self.row(0, forged_log=True)["detail"])


class BridgeTest(unittest.TestCase):
    def tearDown(self) -> None:
        for name in ("discord.gateway", "discord.client"):
            log = logging.getLogger(name)
            log.handlers.clear()
            log.propagate = True
        logging.getLogger().handlers[:] = [h for h in logging.getLogger().handlers
                                           if not getattr(h, "_test_root", False)]

    def start(self) -> io.StringIO:
        """The bridge's real startup with the connection and the instance lock stubbed out."""
        out, order = io.StringIO(), []
        with mock.patch.object(sys, "stdout", out), \
             mock.patch.object(bridge, "_single_instance_acquire", lambda *_: order.append("lock")), \
             mock.patch.object(bridge.client, "run", lambda *a, **k: order.append(
                 ("run", bool(logging.getLogger("discord.gateway").handlers)))):
            bridge._run_bridge()
        self.assertEqual(order, ["lock", ("run", True)])
        return out

    def test_startup_logs_close_codes_on_both_loggers_and_no_payload_anywhere(self) -> None:
        root_seen = io.StringIO()
        root = logging.StreamHandler(root_seen)
        root._test_root = True
        logging.getLogger().addHandler(root)
        logging.getLogger().setLevel(logging.DEBUG)
        out = self.start()
        gw, cl = logging.getLogger("discord.gateway"), logging.getLogger("discord.client")
        gw.debug("Websocket closed with %s, attempting a reconnect.", 4000)
        gw.debug("For Shard ID %s: WebSocket Event: %s", None, PAYLOAD)
        cl.debug("Got a request to %s the websocket.", "RESUME")
        cl.debug("Dispatching event %s", "message")
        gw.info("Shard ID %s session has been invalidated.", None)
        text = out.getvalue()
        self.assertIn("Websocket closed with 4000", text)
        self.assertIn("Got a request to RESUME the websocket", text)
        self.assertIn("session has been invalidated", text)
        self.assertNotIn("SECRET_BODY", text + root_seen.getvalue())
        self.assertNotIn("Dispatching event", text)

    def test_the_templates_are_discord_py_s_own(self) -> None:
        pkg = Path(sys.modules["discord"].__file__).parent
        source = (pkg / "gateway.py").read_text() + (pkg / "client.py").read_text()
        for msg in bridge.GATEWAY_REASON_TEMPLATES:
            self.assertIn(repr(msg)[1:-1], source)

    def test_on_ready_records_one_session(self) -> None:
        record = Path(tempfile.mkdtemp()) / "discord-bridge.sessions.json"

        class _Loop:
            @staticmethod
            def create_task(coro):
                coro.close()

        class _Client:
            user = "BotUser#1"
            loop = _Loop()

            async def change_presence(self, **_):
                return None

        with mock.patch.object(bridge, "client", _Client()), \
             mock.patch.object(bridge, "_poll_loops_started", True), \
             mock.patch.object(bridge, "_recover_orphan_sending_files", lambda *a, **k: None), \
             mock.patch.object(bridge, "discord_config", mock.MagicMock()), \
             mock.patch.object(bridge, "GATEWAY_SESSIONS_FILE", record):
            asyncio.run(bridge.on_ready())
        self.assertEqual(gateway_sessions.count_recent(record, time.time()), 1)


if __name__ == "__main__":
    unittest.main()

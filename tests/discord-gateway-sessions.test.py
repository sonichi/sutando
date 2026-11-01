#!/usr/bin/env python3
"""A Discord bridge that keeps opening fresh gateway sessions is flagged, and says why it dropped.

The heartbeat stays fresh through such a loop, so health-check reads the session record the bridge
alone writes (src/gateway_sessions.py), never its log, which also carries message bodies. The bridge
logs discord.py's close and timeout records, and no raw gateway payload.

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

import ast
import importlib.util
import io
import json
import logging
import sys
import unittest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import gateway_sessions  # noqa: E402

spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

BRIDGE_SRC = (REPO / "src" / "discord-bridge.py").read_text(encoding="utf-8")
BRIDGE_TREE = ast.parse(BRIDGE_SRC)
NOW = 1_793_000_000.0


def _bridge_helpers() -> dict:
    """The reason filter and logger setup, AST-extracted: importing the bridge needs discord.py."""
    keep = [n for n in BRIDGE_TREE.body
            if (isinstance(n, ast.FunctionDef) and n.name in ("_gateway_reason_filter", "_log_gateway_reasons"))
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "GATEWAY_REASON_TEMPLATES"
                                                   for t in n.targets))]
    ns: dict = {"sys": sys}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<bridge>", "exec"), ns)
    return ns


def _calls_in(func_name: str, tree: ast.AST) -> list[str]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return [ast.unparse(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)]
    raise AssertionError(f"{func_name} not found")


class SessionRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "state" / "discord-bridge.sessions.json"

    def test_sessions_in_the_last_hour_are_counted(self) -> None:
        for minutes in (90, 50, 30, 10, 1):
            gateway_sessions.record(self.path, NOW - minutes * 60)
        self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 4)

    def test_a_future_time_is_not_counted(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"sessions": [NOW + 3600] * 9}), encoding="utf-8")
        self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 0)

    def test_a_damaged_record_counts_nothing(self) -> None:
        self.path.parent.mkdir(parents=True)
        for body in ("not json", "[]", '{"sessions": "x"}', '{"sessions": [true, "1", NaN, Infinity]}'):
            with self.subTest(body=body):
                self.path.write_text(body, encoding="utf-8")
                self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 0)
        gateway_sessions.record(self.path, NOW)
        self.assertEqual(json.loads(self.path.read_text())["sessions"], [NOW])

    def test_the_record_is_bounded(self) -> None:
        for i in range(gateway_sessions.KEPT + 50):
            gateway_sessions.record(self.path, NOW + i * 0.01)
        self.assertEqual(len(json.loads(self.path.read_text())["sessions"]), gateway_sessions.KEPT)
        gateway_sessions.record(self.path, NOW + gateway_sessions.WINDOW_S + 10)
        self.assertEqual(len(json.loads(self.path.read_text())["sessions"]), 1)

    def test_a_missing_record_counts_nothing(self) -> None:
        self.assertEqual(gateway_sessions.count_recent(self.path, NOW), 0)


class HealthDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp())

    def sessions(self, n: int) -> None:
        """n new sessions in the last hour, recorded in time order as the bridge does."""
        for i in range(n):
            self.made = getattr(self, "made", 0) + 1
            gateway_sessions.record(self.state / "discord-bridge.sessions.json", NOW - 3000 + 60 * self.made)

    def test_the_threshold_is_six_an_hour(self) -> None:
        self.assertEqual(hc.DISCORD_FLAP_SESSIONS_PER_HOUR, 6)
        self.sessions(5)
        self.assertIsNone(hc._discord_flap_detail(self.state, NOW))
        self.sessions(1)
        self.assertIn("6 new sessions in the last hour", hc._discord_flap_detail(self.state, NOW))

    def test_forged_log_lines_count_nothing(self) -> None:
        """A message body that prints session-shaped lines into the log has no say."""
        forged = "\n".join(f"2099-01-01T00:00:0{i} Discord bridge ready: x (gateway session #{i})"
                           for i in range(9))
        (self.state.parent / "discord-bridge.log").write_text(forged, encoding="utf-8")
        self.assertIsNone(hc._discord_flap_detail(self.state, NOW))

    def test_run_all_checks_consults_it_for_the_discord_bridge(self) -> None:
        tree = ast.parse((REPO / "src" / "health-check.py").read_text(encoding="utf-8"))
        self.assertIn("_discord_flap_detail", _calls_in("run_all_checks", tree))


class BridgeLoggingTest(unittest.TestCase):
    def record(self, level: int, msg: str, *args) -> logging.LogRecord:
        return logging.LogRecord("discord.gateway", level, __file__, 1, msg, args, None)

    def test_close_and_timeout_records_pass_and_payloads_do_not(self) -> None:
        ns = _bridge_helpers()
        keep = ns["_gateway_reason_filter"]
        for msg in ns["GATEWAY_REASON_TEMPLATES"]:
            with self.subTest(msg=msg):
                self.assertTrue(keep(self.record(logging.DEBUG, msg)))
        self.assertFalse(keep(self.record(logging.DEBUG, "For Shard ID %s: WebSocket Event: %s", None, "{}")))
        self.assertFalse(keep(self.record(logging.DEBUG, "Dispatching event %s", "message")))
        self.assertTrue(keep(self.record(logging.INFO, "Shard ID %s session has been invalidated.", None)))

    def test_the_handler_writes_a_close_code_and_nothing_else(self) -> None:
        ns = _bridge_helpers()
        out = io.StringIO()
        ns["_log_gateway_reasons"](out)
        try:
            log = logging.getLogger("discord.gateway")
            log.debug("Websocket closed with %s, attempting a reconnect.", 4000)
            log.debug("For Shard ID %s: WebSocket Event: %s", None, '{"content": "secret body"}')
        finally:
            for name in ("discord.gateway", "discord.client"):
                logging.getLogger(name).handlers.clear()
        self.assertIn("Websocket closed with 4000", out.getvalue())
        self.assertNotIn("secret body", out.getvalue())

    def test_the_templates_are_discord_py_s_own(self) -> None:
        try:
            import discord  # noqa: F401
        except ImportError:
            self.skipTest("discord.py is not installed here")
        pkg = Path(sys.modules["discord"].__file__).parent
        text = (pkg / "gateway.py").read_text() + (pkg / "client.py").read_text()
        for msg in _bridge_helpers()["GATEWAY_REASON_TEMPLATES"]:
            self.assertIn(repr(msg)[1:-1], text)

    def test_startup_and_ready_wire_both_in(self) -> None:
        main_calls = [ast.unparse(c.func) for c in ast.walk(BRIDGE_TREE) if isinstance(c, ast.Call)]
        self.assertIn("_log_gateway_reasons", main_calls)
        run = BRIDGE_SRC.index("client.run(TOKEN")
        self.assertLess(BRIDGE_SRC.rindex("_log_gateway_reasons()", 0, run), run)
        self.assertIn("gateway_sessions.record", _calls_in("on_ready", BRIDGE_TREE))


if __name__ == "__main__":
    unittest.main()

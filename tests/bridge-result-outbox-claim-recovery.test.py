#!/usr/bin/env python3
"""BEHAVIOURAL: Discord's and Slack's result loops retire the claim a crash left on a terminal item.

DesignAClaimBackend.complete() writes the terminal status, then releases the claim. A bridge
that dies between the two leaves a claim that no later claim() reaches: claim_for_send returns
early on DELIVERED or PARKED. Only recover() does, and neither bridge called it on its result
outbox (#5396, the gateway's sibling sites).

recover() releases a claim only on a TERMINAL item whose owner is DEAD past the reclaim TTL, so
the owner must genuinely die here (a child process). Each case drives one real pass of the
bridge's own loop: Discord's poll_results, Slack's result_watcher.

Run: python3 tests/bridge-result-outbox-claim-recovery.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import asyncio
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "packages" / "ag2-sparrow"
sys.path[:0] = [str(REPO / "src"), str(PKG)]
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="bridge-claim-recovery-cfg-")
for _var, _val in (("DISCORD_BOT_TOKEN", "test-not-real"), ("SLACK_BOT_TOKEN", "xoxb-test-not-real"),
                   ("SLACK_APP_TOKEN", "xapp-test-not-real")):
    os.environ.setdefault(_var, _val)
_discord_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_discord_cfg.mkdir(parents=True, exist_ok=True)
(_discord_cfg / "access.json").write_text(json.dumps({"allowFrom": []}))
_slack_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_slack_cfg.mkdir(parents=True, exist_ok=True)
(_slack_cfg / "access.json").write_text(json.dumps({"allowFrom": []}))

for _name in ("slack_bolt", "slack_bolt.adapter", "slack_bolt.adapter.socket_mode",
              "slack_sdk", "slack_sdk.errors"):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        if _name == "slack_bolt":
            _m.App = type("App", (), {"__init__": lambda self, **kw: None,
                                      "event": lambda self, *a, **k: (lambda fn: fn),
                                      "client": types.SimpleNamespace()})
        if _name == "slack_bolt.adapter.socket_mode":
            _m.SocketModeHandler = type("SocketModeHandler", (), {"__init__": lambda self, *a, **kw: None})
        if _name == "slack_sdk.errors":
            _m.SlackApiError = type("SlackApiError", (Exception,), {})
        sys.modules[_name] = _m
try:
    import discord  # noqa: F401
except ImportError:
    _d = types.ModuleType("discord")
    _d.Intents = type("I", (), {"default": staticmethod(lambda: type("X", (), {"message_content": False})())})
    _d.Client = type("C", (), {"__init__": lambda self, **k: None, "event": staticmethod(lambda fn: fn)})
    _d.File, _d.Message, _d.DMChannel = (type(n, (), {}) for n in ("F", "M", "DM"))
    sys.modules["discord"] = _d

from ag2_sparrow import outbox  # noqa: E402
from ag2_sparrow.delivery_core import DesignAClaimBackend  # noqa: E402

import discord_result_delivery as drd  # noqa: E402
import slack_result_delivery as srd  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Claim, write the terminal status, and die before the release: complete()'s crash window.
_CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {pkg!r})
    from pathlib import Path
    from ag2_sparrow import outbox
    from ag2_sparrow.delivery_core import DesignAClaimBackend
    root, item, worker = Path({root!r}), {item!r}, {worker!r}
    backend = DesignAClaimBackend(root)
    backend.publish(item, b"")
    assert backend.claim(item, worker) is not None
    with outbox._item_lock(root, item):
        outbox.record_delivered(root, item, provider=worker)
    os._exit(0)
    """
)


class _Stop(Exception):
    """Breaks a bridge loop after exactly one pass."""


class _Recovery:
    """Shared cases; a subclass names its delivery module and drives one pass of its loop."""

    mod = None

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="bridge-claim-recovery-"))
        self.results = self.td / "results"
        (self.results / "archive").mkdir(parents=True)
        (self.td / "tasks" / "archive").mkdir(parents=True)
        (self.td / "state").mkdir()
        self.mod._LAST_RECOVER.clear()
        self.addCleanup(self.mod._LAST_RECOVER.clear)

    def root(self) -> Path:
        return self.mod.result_backend(self.results).root

    def crash(self, item: str, age_s: float = 3600.0) -> None:
        proc = subprocess.run([sys.executable, "-c", _CHILD.format(
            pkg=str(PKG), root=str(self.root()), item=item, worker=self.mod.WORKER)],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        rec = outbox.read_delivery_claim(self.root(), item)
        self.assertIsNotNone(rec, "premise: the crash leaves the claim")
        with self.assertRaises(OSError, msg="premise: the claiming pid is genuinely dead"):
            os.kill(rec.pid, 0)
        self.age(item, age_s)

    def age(self, item: str, age_s: float) -> None:
        for p in outbox._claims_dir(self.root()).glob("*.claim"):
            d = json.loads(p.read_text())
            if d.get("item_id") == item:
                d["claimed_at"] = time.time() - age_s
                p.write_text(json.dumps(d))

    def held(self, item: str) -> bool:
        return outbox.read_delivery_claim(self.root(), item) is not None

    def test_one_pass_retires_a_dead_owners_claim_on_a_delivered_item(self):
        self.crash("task-dead-terminal")
        log = self.one_pass()
        self.assertFalse(self.held("task-dead-terminal"))
        self.assertEqual(outbox.item_status(self.root(), "task-dead-terminal"), "DELIVERED",
                         "retiring the claim must not change the item")
        self.assertIn("retired 1", log)
        self.assertIn("task-dead-terminal", log)

    def test_a_claim_inside_the_reclaim_ttl_is_left_alone(self):
        self.crash("task-fresh", age_s=0.0)
        self.one_pass()
        self.assertTrue(self.held("task-fresh"), "recover() must honour the reclaim TTL")

    def test_a_live_owners_claim_on_a_terminal_item_is_never_taken(self):
        backend = DesignAClaimBackend(self.root())
        backend.publish("task-live", b"")
        self.assertIsNotNone(backend.claim("task-live", self.mod.WORKER))
        with outbox._item_lock(self.root(), "task-live"):
            outbox.record_delivered(self.root(), "task-live", provider=self.mod.WORKER)
        self.age("task-live", 3600.0)
        self.one_pass()
        self.assertTrue(self.held("task-live"), "a running sender is never displaced")

    def test_a_failing_recovery_is_reported_and_the_pass_goes_on(self):
        def broken(_results_dir):
            raise OSError("outbox unreadable")
        real, self.mod.recover_claims = self.mod.recover_claims, broken
        self.addCleanup(setattr, self.mod, "recover_claims", real)
        log = self.one_pass()               # one_pass reaches the loop's sleep, so the pass completed
        self.assertIn("claim recovery failed (OSError: outbox unreadable); delivery continues", log)

    def test_the_sweep_repeats_past_the_ttl_not_on_every_pass(self):
        self.one_pass()                      # the first pass sweeps at once
        self.crash("task-later")
        self.one_pass()
        self.assertTrue(self.held("task-later"), "within RECOVER_EVERY_S the next pass does not sweep")
        self.assertGreater(self.mod.RECOVER_EVERY_S, DesignAClaimBackend(self.root()).reclaim_ttl_s,
                           "the cadence outlasts the reclaim TTL, or a sweep could never retire anything")
        for key in list(self.mod._LAST_RECOVER):
            self.mod._LAST_RECOVER[key] -= self.mod.RECOVER_EVERY_S
        self.one_pass()
        self.assertFalse(self.held("task-later"), "once it is due again, the sweep runs")


class DiscordPollResults(_Recovery, unittest.TestCase):
    mod = drd

    @classmethod
    def setUpClass(cls):
        try:
            cls.db = _load("db_claim_recovery", REPO / "src" / "discord-bridge.py")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            raise unittest.SkipTest(f"discord-bridge not importable here: {str(e)[:60]}")

    def one_pass(self) -> str:
        db = self.db
        db.RESULTS_DIR, db.TASKS_DIR = self.results, self.td / "tasks"
        db.ARCHIVE_RESULTS_DIR = self.results / "archive"

        class _Client:
            def is_ready(self):
                return False

        async def _sleep(_s):
            raise _Stop()
        db.client, db._recovered_replies = _Client(), {}
        db.pending_replies.clear()
        db.save_pending_replies = lambda *a, **k: None
        real_sleep, db.asyncio.sleep = db.asyncio.sleep, _sleep
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                asyncio.run(db.poll_results())
        except _Stop:
            pass
        finally:
            db.asyncio.sleep = real_sleep
        return out.getvalue()


class SlackResultWatcher(_Recovery, unittest.TestCase):
    mod = srd

    @classmethod
    def setUpClass(cls):
        cls.sb = _load("sb_claim_recovery", REPO / "src" / "slack-bridge.py")

    def one_pass(self) -> str:
        sb = self.sb
        sb.REPO = self.td
        sb.RESULTS_DIR, sb.TASKS_DIR, sb.STATE_DIR = self.results, self.td / "tasks", self.td / "state"
        sb.ARCHIVE_RESULTS_DIR = self.results / "archive"
        sb.ARCHIVE_TASKS_DIR = self.td / "tasks" / "archive"
        sb.pending_replies.clear()

        def _sleep(_s):
            raise _Stop()
        real_sleep, sb.time.sleep = sb.time.sleep, _sleep
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), self.assertRaises(_Stop):
                sb.result_watcher()
        finally:
            sb.time.sleep = real_sleep
        return out.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Mention-gate (ON side) contract: while ON, a message @-tagging the owner in
a requireMention channel counts as a bot mention and is ingested + audit-
logged; OFF and missing/malformed state keep today's behavior (fail-CLOSED —
the feature adds ingestion, so every failure reads OFF); expiry flips ON→OFF
automatically; bot-mention messages are unaffected in both states. Plus the
Discord delegation pins: the production `_mention_gate_triggers_ingest`
chokepoint and an AST pin that the requireMention rejection branch consults it.

Run: python3 tests/mention-gate.test.py
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

REPO = Path(__file__).resolve().parent.parent

# ISOLATE BEFORE THE BRIDGE IMPORT. discord-bridge resolves channel config and
# workspace at module scope, so a later assignment would read the operator's home.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-mention-gate-")
os.environ.pop("CLAUDE_HOME", None)
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ["SUTANDO_WORKSPACE"] = tempfile.mkdtemp(prefix="ws-mention-gate-")
_WS = Path(os.environ["SUTANDO_WORKSPACE"]).resolve()  # bridge resolves symlinks (/var -> /private/var)

_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
# Fixture ids only — 111222333 is the "owner", 444555666 stays team, 777000111 is the bot.
(_cfg / "access.json").write_text(
    '{"allowFrom": ["111222333", "444555666"],'
    ' "tierMap": {"111222333": "owner", "444555666": "team"}}',
    encoding="utf-8")
(_cfg / ".env").write_text("DISCORD_BOT_TOKEN=test-token\n", encoding="utf-8")


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mg = _load("mention_gate", "src/mention_gate.py")
db = _load("db_mention_gate", "src/discord-bridge.py")

OWNERS = ["111222333", "999888777"]


def _msg(author_id="123456789012345678", content="", mention_ids=(),
         channel_id="555666777", message_id="424242"):
    return NS(
        author=NS(id=author_id),
        content=content,
        mentions=[NS(id=m) for m in mention_ids],
        channel=NS(id=channel_id, name="general"),
        id=message_id,
    )


class GateStatePolicy(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_default_missing_state_is_off(self):
        # Fail-closed control: no state file = today's behavior, no ingestion.
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws))
        self.assertEqual(mg.read_state(self.ws),
                         {"mentions_enabled": False, "until": None})

    def test_on_triggers_and_off_stops(self):
        mg.write_state(self.ws, mentions_enabled=True)
        self.assertTrue(mg.owner_tag_triggers_ingest(self.ws))
        mg.write_state(self.ws, mentions_enabled=False)
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws))

    def test_expiry_flips_on_to_off_automatically(self):
        mg.write_state(self.ws, mentions_enabled=True,
                       until="2026-01-01T00:00:00Z")
        before = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
        after = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        self.assertTrue(mg.owner_tag_triggers_ingest(self.ws, now=before))
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws, now=after))

    def test_unparseable_until_fails_closed(self):
        mg.write_state(self.ws, mentions_enabled=True, until="whenever")
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws))

    def test_malformed_state_fails_closed(self):
        state = self.ws / "state" / "mention-gate.json"
        state.parent.mkdir(parents=True)
        for garbage in ("not json {", '{"mentions_enabled": "yes"}', ""):
            state.write_text(garbage, encoding="utf-8")
            self.assertEqual(mg.read_state(self.ws),
                             {"mentions_enabled": False, "until": None}, garbage)
            self.assertFalse(mg.owner_tag_triggers_ingest(self.ws), garbage)

    def test_write_is_atomic_temp_plus_replace(self):
        with mock.patch("os.replace", wraps=os.replace) as rep:
            path = mg.write_state(self.ws, mentions_enabled=True, until=None)
        rep.assert_called_once()
        self.assertEqual(rep.call_args.args[1], path)
        self.assertEqual(json.loads(path.read_text()),
                         {"mentions_enabled": True, "until": None})
        # A failed swap must leave the previous state intact and no temp behind.
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mg.write_state(self.ws, mentions_enabled=False)
        self.assertEqual(json.loads(path.read_text()),
                         {"mentions_enabled": True, "until": None})
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])


class TaggingDetection(unittest.TestCase):
    def test_platform_mentions_array_hits(self):
        self.assertTrue(mg.message_tags_owner(["111222333"], "no tokens here", OWNERS))
        self.assertTrue(mg.message_tags_owner(["444555666", "999888777"], "", OWNERS))

    def test_text_fallback_covers_both_forms(self):
        self.assertTrue(mg.message_tags_owner([], "hi <@111222333>", OWNERS))
        self.assertTrue(mg.message_tags_owner([], "hi <@!999888777>", OWNERS))

    def test_non_owner_and_bot_only_do_not_tag(self):
        self.assertFalse(mg.message_tags_owner(["777000111"], "<@777000111> do x", OWNERS))
        self.assertFalse(mg.message_tags_owner([], "plain text", OWNERS))
        self.assertFalse(mg.message_tags_owner(["111222333"], "anything", []))


class AuditLog(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.ws = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_append_order_and_count(self):
        self.assertEqual(mg.gated_ingest_count(self.ws), 0)
        for i in range(3):
            mg.log_gated_ingest(self.ws, {"message_id": str(i)})
        self.assertEqual(mg.gated_ingest_count(self.ws), 3)
        path = self.ws / "state" / "mention-gate-ingested.jsonl"
        rows = [json.loads(ln) for ln in path.read_text().splitlines()]
        self.assertEqual([r["message_id"] for r in rows], ["0", "1", "2"])


class DiscordTriggerPin(unittest.TestCase):
    """The production chokepoint — not a copy — applies the shared policy."""

    def setUp(self):
        self.assertEqual(db.REPO, _WS)  # pins hold only against the isolated workspace
        for name in ("mention-gate.json", "mention-gate-ingested.jsonl"):
            p = _WS / "state" / name
            if p.exists():
                p.unlink()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)

    def test_on_owner_tagged_no_bot_mention_is_admitted_and_audited(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(content="<@111222333> look at this", mention_ids=["111222333"])
        self.assertTrue(db._mention_gate_triggers_ingest(m))
        self.assertEqual(mg.gated_ingest_count(_WS), 1)
        path = _WS / "state" / "mention-gate-ingested.jsonl"
        row = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(row["message_id"], "424242")
        self.assertEqual(row["channel_id"], "555666777")
        self.assertIn("look at this", row["body"])

    def test_on_text_only_owner_token_is_still_caught(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(content="fyi <@!111222333>", mention_ids=[])
        self.assertTrue(db._mention_gate_triggers_ingest(m))

    def test_off_same_message_stays_invisible(self):
        mg.write_state(_WS, mentions_enabled=False)
        m = _msg(content="<@111222333> look at this", mention_ids=["111222333"])
        self.assertFalse(db._mention_gate_triggers_ingest(m))
        self.assertEqual(mg.gated_ingest_count(_WS), 0)

    def test_default_missing_state_stays_invisible(self):
        # The fail-closed control: no state file at all = today's behavior.
        m = _msg(content="<@111222333> look at this", mention_ids=["111222333"])
        self.assertFalse(db._mention_gate_triggers_ingest(m))
        self.assertEqual(mg.gated_ingest_count(_WS), 0)

    def test_on_message_tagging_neither_never_triggers(self):
        mg.write_state(_WS, mentions_enabled=True)
        self.assertFalse(db._mention_gate_triggers_ingest(
            _msg(content="just chatting")))
        self.assertFalse(db._mention_gate_triggers_ingest(
            _msg(content="<@777000111> do x", mention_ids=["777000111"])))
        self.assertEqual(mg.gated_ingest_count(_WS), 0)

    def test_owner_authored_message_never_triggers(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(author_id="111222333", content="note <@111222333>",
                 mention_ids=["111222333"])
        self.assertFalse(db._mention_gate_triggers_ingest(m))

    def test_fail_closed_when_audit_logging_breaks(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(content="<@111222333> hi", mention_ids=["111222333"])
        with mock.patch.object(db.mention_gate, "log_gated_ingest",
                               side_effect=OSError("disk full")):
            self.assertFalse(db._mention_gate_triggers_ingest(m))  # rejection stands

    def test_require_mention_branch_consults_the_gate(self):
        """AST pin: the `require_mention and not bot_mentioned and not
        role_mentioned` rejection consults the gate before returning, and only
        there — bot-mention messages never reach it in either state."""
        src = (REPO / "src" / "discord-bridge.py").read_text()
        tree = ast.parse(src)
        handler = next(n for n in tree.body
                       if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == "_handle_discord_message")
        hits = []
        for node in ast.walk(handler):
            if not isinstance(node, ast.If):
                continue
            test_names = {c.id for c in ast.walk(node.test) if isinstance(c, ast.Name)}
            if {"require_mention", "bot_mentioned", "role_mentioned"} <= test_names:
                sub = list(ast.walk(node))
                calls_gate = any(
                    isinstance(c, ast.Call)
                    and getattr(c.func, "id", getattr(c.func, "attr", None))
                    == "_mention_gate_triggers_ingest"
                    for c in sub)
                has_return = any(isinstance(c, ast.Return) for c in sub)
                if calls_gate and has_return:
                    hits.append(node.lineno)
        self.assertTrue(hits, "the requireMention rejection no longer consults the gate")
        write_line = next(
            n.lineno for n in ast.walk(handler)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == "_write_task_file")
        self.assertLess(hits[0], write_line,
                        "gate must run at the accept decision, before the task write")


class CliRoundTrip(unittest.TestCase):
    CLI = REPO / "skills" / "mention-gate" / "scripts" / "mention-gate.py"

    def _run(self, ws, *args):
        env = dict(os.environ, SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(ws))
        return subprocess.run([sys.executable, str(self.CLI), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_default_status_is_off_then_on_for_then_off(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("OFF (default)", self._run(td, "status").stdout)
            r = self._run(td, "on", "--for", "30m")
            self.assertEqual(r.returncode, 0, r.stderr)
            state = mg.read_state(Path(td))
            self.assertTrue(state["mentions_enabled"])
            self.assertTrue(state["until"])
            self.assertIn("ON", self._run(td, "status").stdout)
            mg.log_gated_ingest(Path(td), {"message_id": "1", "body": "x"})
            self.assertIn("1 message(s)", self._run(td, "status").stdout)
            self.assertEqual(self._run(td, "off").returncode, 0)
            self.assertIn("OFF", self._run(td, "status").stdout)

    def test_bad_duration_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run(td, "on", "--for", "soon")
            self.assertNotEqual(r.returncode, 0)
            # A refused command must not have flipped the gate on.
            self.assertFalse(mg.read_state(Path(td))["mentions_enabled"])


if __name__ == "__main__":
    unittest.main()

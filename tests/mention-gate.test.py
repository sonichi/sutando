#!/usr/bin/env python3
"""Mention-gate (ON side) contract: while ON, a message @-tagging the owner in
a requireMention channel counts as a bot mention and is ingested; OFF and
missing/malformed state keep today's behavior (fail-CLOSED). Review controls
pinned (#3473 @qingyun-wu): (1) an explicitly EMPTY tierMap yields an EMPTY
owner set — no allowFrom fallback escalation; (2) the audit row binds to the
DURABLY WRITTEN task, not the gate verdict — an unauthorized sender leaves no
audit row and no task. Includes full-handler behavioral drives plus the
production-helper delegation pins and AST placement pins.

Run: python3 tests/mention-gate.test.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
import discord  # noqa: E402  — the bridge imported it (or re-exec'd), so it resolves here

OWNERS = ["111222333", "999888777"]
STRANGER = "123456789012345678"


def _msg(author_id=STRANGER, content="", mention_ids=(),
         channel_id="555666777", message_id="424242"):
    return NS(
        author=NS(id=author_id),
        content=content,
        mentions=[NS(id=m) for m in mention_ids],
        channel=NS(id=channel_id, name="general"),
        id=message_id,
    )


def _audit_rows():
    path = _WS / "state" / "mention-gate-ingested.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _reset_ws_gate():
    for name in ("mention-gate.json", "mention-gate-ingested.jsonl"):
        p = _WS / "state" / name
        if p.exists():
            p.unlink()


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

    def test_naive_until_and_naive_now_are_treated_as_utc(self):
        mg.write_state(self.ws, mentions_enabled=True,
                       until="2026-01-01T00:00:00")  # no timezone
        self.assertTrue(mg.owner_tag_triggers_ingest(
            self.ws, now=datetime(2025, 12, 31, 23, 0)))  # naive now
        self.assertFalse(mg.owner_tag_triggers_ingest(
            self.ws, now=datetime(2026, 1, 1, 0, 0, 1)))

    def test_unparseable_until_fails_closed(self):
        mg.write_state(self.ws, mentions_enabled=True, until="whenever")
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws))

    def test_empty_string_until_fails_closed(self):
        # "" is a str, so read_state keeps it; the expiry parse must not treat
        # it as "no expiry" and leave the gate open-endedly ON.
        mg.write_state(self.ws, mentions_enabled=True, until="")
        self.assertFalse(mg.owner_tag_triggers_ingest(self.ws))

    def test_malformed_state_fails_closed(self):
        state = self.ws / "state" / "mention-gate.json"
        state.parent.mkdir(parents=True)
        for garbage in ("not json {", '{"mentions_enabled": "yes"}', "", "[1, 2]"):
            state.write_text(garbage, encoding="utf-8")
            self.assertEqual(mg.read_state(self.ws),
                             {"mentions_enabled": False, "until": None}, garbage)
            self.assertFalse(mg.owner_tag_triggers_ingest(self.ws), garbage)

    def test_non_string_until_reads_as_none(self):
        state = self.ws / "state" / "mention-gate.json"
        state.parent.mkdir(parents=True)
        state.write_text('{"mentions_enabled": true, "until": 12345}')
        self.assertEqual(mg.read_state(self.ws),
                         {"mentions_enabled": True, "until": None})
        self.assertTrue(mg.owner_tag_triggers_ingest(self.ws))

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
        # Double fault: even the temp cleanup failing must re-raise the ORIGINAL
        # error, and the previous state still survives.
        with mock.patch("os.replace", side_effect=OSError("disk full")), \
                mock.patch("os.unlink", side_effect=OSError("gone")):
            with self.assertRaises(OSError):
                mg.write_state(self.ws, mentions_enabled=False)
        self.assertEqual(json.loads(path.read_text()),
                         {"mentions_enabled": True, "until": None})


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


class OwnerIdResolution(unittest.TestCase):
    """#3473 P1 control: a PRESENT tierMap is authoritative, even empty."""

    def _ids(self, doc: str):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "access.json"
            p.write_text(doc, encoding="utf-8")
            with mock.patch.object(db, "ACCESS_FILE", p):
                return db._mention_gate_owner_ids()

    def test_reviewers_control_empty_tiermap_yields_no_owners(self):
        # qingyun's exact control: {"allowFrom":["111222333"],"tierMap":{}}
        # returned ['111222333'] before the fix — allowFrom escalation.
        self.assertEqual(
            self._ids('{"allowFrom": ["111222333"], "tierMap": {}}'), [])

    def test_present_tiermap_owner_entries_win_over_allowfrom(self):
        self.assertEqual(
            self._ids('{"allowFrom": ["111222333"],'
                      ' "tierMap": {"999888777": "owner", "111222333": "team"}}'),
            ["999888777"])

    def test_absent_tiermap_falls_back_to_allowfrom(self):
        self.assertEqual(self._ids('{"allowFrom": ["111222333"]}'), ["111222333"])

    def test_non_dict_tiermap_fails_closed(self):
        self.assertEqual(
            self._ids('{"allowFrom": ["111222333"], "tierMap": "owner"}'), [])

    def test_non_dict_document_fails_closed(self):
        self.assertEqual(self._ids('["111222333"]'), [])

    def test_unreadable_file_fails_closed(self):
        with mock.patch.object(db, "ACCESS_FILE", Path("/nonexistent/access.json")):
            self.assertEqual(db._mention_gate_owner_ids(), [])

    def test_empty_tiermap_means_the_gate_never_triggers(self):
        mg.write_state(_WS, mentions_enabled=True)
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "access.json"
                p.write_text('{"allowFrom": ["111222333"], "tierMap": {}}')
                with mock.patch.object(db, "ACCESS_FILE", p):
                    m = _msg(content="<@111222333> hi", mention_ids=["111222333"])
                    self.assertFalse(db._mention_gate_triggers_ingest(m))
        finally:
            mg.write_state(_WS, mentions_enabled=False, until=None)


class DiscordTriggerPin(unittest.TestCase):
    """The production chokepoint — not a copy — applies the shared policy.
    #3473 P1 (2): the VERDICT never writes the audit; only
    _mention_gate_log_admission does, and the handler calls it post-write."""

    def setUp(self):
        self.assertEqual(db.REPO, _WS)  # pins hold only against the isolated workspace
        _reset_ws_gate()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)

    def test_on_owner_tagged_verdict_is_true_but_writes_no_audit(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(content="<@111222333> look at this", mention_ids=["111222333"])
        self.assertTrue(db._mention_gate_triggers_ingest(m))
        self.assertEqual(mg.gated_ingest_count(_WS), 0)  # audit binds to the write

    def test_log_admission_writes_exactly_one_row(self):
        m = _msg(content="<@111222333> look at this", mention_ids=["111222333"])
        db._mention_gate_log_admission(m)
        rows = _audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "424242")
        self.assertEqual(rows[0]["channel_id"], "555666777")
        self.assertIn("look at this", rows[0]["body"])

    def test_log_admission_failure_never_raises(self):
        m = _msg(content="<@111222333> hi", mention_ids=["111222333"])
        with mock.patch.object(db.mention_gate, "log_gated_ingest",
                               side_effect=OSError("disk full")):
            db._mention_gate_log_admission(m)  # best-effort: must not raise
        self.assertEqual(mg.gated_ingest_count(_WS), 0)

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

    def test_verdict_fails_closed_on_any_error(self):
        mg.write_state(_WS, mentions_enabled=True)
        m = _msg(content="<@111222333> hi", mention_ids=["111222333"])
        with mock.patch.object(db.mention_gate, "message_tags_owner",
                               side_effect=RuntimeError("boom")):
            self.assertFalse(db._mention_gate_triggers_ingest(m))  # rejection stands

    def test_require_mention_branch_consults_the_gate(self):
        """AST pins: the requireMention rejection consults the gate; the audit
        call sits AFTER the task write; the verdict helper never audits."""
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

        def _call_line(name):
            return next(
                (n.lineno for n in ast.walk(handler)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", getattr(n.func, "attr", None)) == name),
                None)

        write_line = _call_line("_write_task_file")
        audit_line = _call_line("_mention_gate_log_admission")
        self.assertIsNotNone(write_line)
        self.assertIsNotNone(audit_line, "handler no longer audits gate admissions")
        self.assertLess(hits[0], write_line,
                        "gate must run at the accept decision, before the task write")
        self.assertLess(write_line, audit_line,
                        "audit must bind to the DURABLE write, never precede it")

        verdict_fn = next(n for n in tree.body
                          if isinstance(n, ast.FunctionDef)
                          and n.name == "_mention_gate_triggers_ingest")
        audits_in_verdict = [
            n for n in ast.walk(verdict_fn)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", getattr(n.func, "id", None)) == "log_gated_ingest"]
        self.assertEqual(audits_in_verdict, [],
                         "the verdict helper must not write the audit (#3473 P1)")


class _FakeChannel:
    def __init__(self, cid=555666777):
        self.id = cid
        self.name = "general"
        self.sent = []

    async def send(self, *a, **k):
        self.sent.append(a[0] if a else k)


class _FakeMsg:
    def __init__(self, content, author_id=STRANGER, mention_ids=("111222333",)):
        self.content = content
        self.channel = _FakeChannel()
        self.author = NS(id=int(author_id), bot=False,
                         __str__=lambda self_: "stranger#0001")
        self.mentions = [NS(id=int(m)) for m in mention_ids]
        self.role_mentions = []
        self.embeds = []
        self.attachments = []
        self.message_snapshots = []
        self.type = discord.MessageType.default
        self.reference = None
        self.guild = None
        self.id = 987654321


class HandlerBehavior(unittest.TestCase):
    """Full-handler drives: gate ON admits an authorized owner-tagged message
    (task written, ONE audit row after it); gate OFF/default rejects; an
    unauthorized sender yields neither task nor audit (#3473 control inversion)."""

    def setUp(self):
        _reset_ws_gate()
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.tasks.mkdir()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)
        self._td.cleanup()

    def _drive(self, msg, channel_allow=None):
        """Run the production _handle_discord_message; return (stdout, tasks)."""
        fake_client = NS(user=object())
        cfg = (True, channel_allow if channel_allow is not None else set())
        buf = io.StringIO()

        async def _noop(*a, **k):
            return None

        with mock.patch.object(db, "client", fake_client), \
                mock.patch.object(db, "_observe_for_mod", _noop), \
                mock.patch.object(db, "TASKS_DIR", self.tasks), \
                mock.patch.object(db, "load_channel_config", lambda cid: cfg), \
                contextlib.redirect_stdout(buf):
            try:
                asyncio.run(db._handle_discord_message(msg))
            except Exception:
                # Write + audit precede the live wait-for-result leg, whose
                # deps the fakes lack; assertions key on the durable artifacts.
                pass
        return buf.getvalue(), sorted(p.name for p in self.tasks.glob("task-*.txt"))

    def test_gate_on_authorized_tagged_message_writes_task_then_one_audit_row(self):
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._drive(_FakeMsg("<@111222333> please look at the build"))
        self.assertEqual(len(written), 1, out)
        body = (self.tasks / written[0]).read_text()
        self.assertIn("please look at the build", body)
        rows = _audit_rows()
        self.assertEqual(len(rows), 1, out)
        self.assertEqual(rows[0]["message_id"], "987654321")

    def test_gate_off_same_message_is_not_ingested(self):
        mg.write_state(_WS, mentions_enabled=False)
        out, written = self._drive(_FakeMsg("<@111222333> please look at the build"))
        self.assertEqual(written, [], out)
        self.assertIn("[skip] not mentioned", out)
        self.assertEqual(_audit_rows(), [])

    def test_default_missing_state_same_message_is_not_ingested(self):
        out, written = self._drive(_FakeMsg("<@111222333> please look at the build"))
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])

    def test_unauthorized_sender_leaves_neither_task_nor_audit(self):
        # Reviewer's control inversion: the gate ADMITS past requireMention,
        # but the channel allowlist rejects — no task, and audit unchanged.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._drive(
            _FakeMsg("<@111222333> please look at the build"),
            channel_allow={"444555666"})  # sender not listed, not global-allowed
        self.assertIn("not in channel allowlist", out)
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])


# Subprocess-coverage opt-in (same pattern as tests/census-d1-anchors.test.py):
# the coverage gate sets this so CLI lines exercised via subprocess still count.
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]


class CliRoundTrip(unittest.TestCase):
    CLI = REPO / "skills" / "mention-gate" / "scripts" / "mention-gate.py"

    def _run(self, ws, *args):
        env = dict(os.environ, SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(ws))
        return subprocess.run([*PYBASE, str(self.CLI), *args],
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

    def test_status_reports_an_expired_on_window(self):
        with tempfile.TemporaryDirectory() as td:
            mg.write_state(Path(td), mentions_enabled=True,
                           until="2020-01-01T00:00:00Z")
            out = self._run(td, "status").stdout
            self.assertIn("expired at 2020-01-01T00:00:00Z", out)


class WitnessRunner(unittest.TestCase):
    """The live-witness helper's verdicts, driven hermetically (input stubbed)."""

    wit = _load("mention_gate_witness",
                "skills/mention-gate/scripts/witness.py")

    def _in_ws(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        ws = Path(td.name)
        env = mock.patch.dict(os.environ, {"SUTANDO_WORKSPACE": str(ws)})
        env.start()
        self.addCleanup(env.stop)
        (ws / "tasks").mkdir(parents=True)
        return ws

    def test_precondition_mismatch_exits_2(self):
        self._in_ws()  # gate reads OFF; case2 needs ON
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.wit.main(["case2", "--marker", "m1"]), 2)

    def test_case1_gate_off_no_ingest_passes(self):
        self._in_ws()
        with mock.patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                self.wit.main(["case1", "--marker", "m2", "--timeout", "1"]), 0)

    def test_case2_task_plus_audit_row_passes(self):
        ws = self._in_ws()
        mg.write_state(ws, mentions_enabled=True)
        (ws / "tasks" / "task-1.txt").write_text("task: m3 please look\n")

        def _sent(_prompt=""):
            mg.log_gated_ingest(ws, {"message_id": "1"})  # the admission lands
            return ""

        with mock.patch("builtins.input", side_effect=_sent), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                self.wit.main(["case2", "--marker", "m3", "--timeout", "5"]), 0)

    def test_marker_scan_skips_unreadable_task_files(self):
        ws = self._in_ws()
        (ws / "tasks" / "task-3.txt").write_text("task: m5 readable\n")
        unreadable = ws / "tasks" / "task-4.txt"
        unreadable.write_text("task: m5 secret\n")
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o600)
        hits = self.wit._tasks_with_marker(ws, "m5")
        self.assertEqual([p.name for p in hits], ["task-3.txt"])

    def test_case3_unauthorized_ingest_would_fail(self):
        ws = self._in_ws()
        mg.write_state(ws, mentions_enabled=True)
        (ws / "tasks" / "task-2.txt").write_text("task: m4 sneaky\n")  # ingested = bad
        with mock.patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                self.wit.main(["case3", "--marker", "m4", "--timeout", "1"]), 1)


class EditIntroducesTheTag(unittest.TestCase):
    """An owner tag added by an EDIT must reach the same gate as one that
    arrived with the message. The bridge already reprocesses edits, but Case 1
    asked `_message_mentions_bot`, which the gate is precisely what stands in
    for — so a tag typed in two minutes later was judged by a different rule."""

    _next_id = 550000000

    def setUp(self):
        _reset_ws_gate()
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.tasks.mkdir()
        # seen_message_ids is a module global; a shared fake id would dedup a
        # later test's message and read as a gate failure.
        db.seen_message_ids.clear()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)
        db.seen_message_ids.clear()
        self._td.cleanup()

    def _edit(self, before_content, after_content, allow=None, require_mention=True):
        """Drive the production on_message_edit; return (stdout, tasks)."""
        before, after = _FakeMsg(before_content), _FakeMsg(after_content)
        EditIntroducesTheTag._next_id += 1
        before.id = after.id = EditIntroducesTheTag._next_id
        self._require_mention = require_mention
        before.mentions = [NS(id=111222333)] if "<@111222333>" in before_content else []
        after.mentions = [NS(id=111222333)] if "<@111222333>" in after_content else []
        fake_client = NS(user=object())
        cfg = (require_mention, allow if allow is not None else {str(STRANGER)})
        buf = io.StringIO()

        async def _noop(*a, **k):
            return None

        with mock.patch.object(db, "client", fake_client), \
                mock.patch.object(db, "_observe_for_mod", _noop), \
                mock.patch.object(db, "TASKS_DIR", self.tasks), \
                mock.patch.object(db, "load_channel_config", lambda cid: cfg), \
                contextlib.redirect_stdout(buf):
            try:
                asyncio.run(db.on_message_edit(before, after))
            except Exception:
                pass
        return buf.getvalue(), sorted(p.name for p in self.tasks.glob("task-*.txt"))

    def test_an_edit_that_adds_the_owner_tag_is_ingested_while_the_gate_is_on(self):
        # Measured 2026-08-29: posted untagged at 17:28, tag appended at 17:30.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._edit("here are the six launch videos",
                                  "here are the six launch videos <@111222333>")
        self.assertEqual(len(written), 1, out)
        self.assertEqual(len(_audit_rows()), 1, out)

    def test_the_same_edit_is_not_ingested_while_the_gate_is_off(self):
        # Fail-closed is the whole design: OFF must keep today's behavior.
        mg.write_state(_WS, mentions_enabled=False)
        out, written = self._edit("here are the six launch videos",
                                  "here are the six launch videos <@111222333>")
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])

    def test_an_edit_to_an_already_tagged_message_does_not_ingest_twice(self):
        # The guard that makes this safe: `before` already counted, so the edit
        # introduced nothing. Without it every later typo re-queues the message.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._edit("<@111222333> take a look",
                                  "<@111222333> take a look please")
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])

    def test_an_edit_adding_no_tag_at_all_is_still_ignored(self):
        # Negative control: without it the predicate could admit every edit.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._edit("first draft", "second draft")
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])

    def test_an_unauthorized_sender_edit_leaves_neither_task_nor_audit(self):
        # The allowlist still runs after the gate admits, exactly as on arrival.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._edit("nothing yet", "<@111222333> now tagged",
                                  allow={"999999999"})
        self.assertEqual(written, [], out)
        self.assertEqual(_audit_rows(), [])


class FreeListenChannelIsNotDoubleIngested(unittest.TestCase):
    """`requireMention:false` is the adjacent value the edit cases all fixed.

    Arrival consults the gate ONLY inside its requireMention branch, so a
    predicate that consults it unconditionally re-queues a message a free-listen
    channel had already ingested — one task on arrival, a second on the edit.
    """

    def setUp(self):
        _reset_ws_gate()
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.tasks.mkdir()
        db.seen_message_ids.clear()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)
        db.seen_message_ids.clear()
        self._td.cleanup()

    def _arrive_then_edit(self, require_mention):
        """Production on_message THEN on_message_edit, one authorized human."""
        before = _FakeMsg("here are the six launch videos")
        after = _FakeMsg("here are the six launch videos <@111222333>")
        before.mentions, after.mentions = [], [NS(id=111222333)]
        before.id = after.id = 770000001 if require_mention else 770000002
        cfg = (require_mention, {str(STRANGER)})
        fake_client = NS(user=object())

        async def _noop(*a, **k):
            return None

        buf = io.StringIO()
        with mock.patch.object(db, "client", fake_client), \
                mock.patch.object(db, "_observe_for_mod", _noop), \
                mock.patch.object(db, "TASKS_DIR", self.tasks), \
                mock.patch.object(db, "load_channel_config", lambda cid: cfg), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            for coro in (db._handle_discord_message(before),
                         db.on_message_edit(before, after)):
                try:
                    asyncio.run(coro)
                except Exception:
                    pass
        return buf.getvalue(), sorted(p.name for p in self.tasks.glob("task-*.txt"))

    def test_free_listen_arrival_plus_edit_is_exactly_one_task(self):
        # The reviewer's control: parent gave 1 then 2; this must stay at 1.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._arrive_then_edit(require_mention=False)
        self.assertEqual(len(written), 1,
                         f"free-listen must not re-queue on edit, got {written}\n{out}")

    def test_require_mention_channel_still_ingests_on_the_edit(self):
        # The positive control that keeps the scoping from disabling the fix:
        # here arrival skips and the edit is the ONLY thing that can ingest.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._arrive_then_edit(require_mention=True)
        self.assertEqual(len(written), 1, out)
        self.assertEqual(len(_audit_rows()), 1, out)


class DMIsNotTheGatesTerritory(unittest.TestCase):
    """Arrival evaluates requireMention and the gate only inside `if not is_dm`.

    An authorized non-owner's DM is ingested on ARRIVAL, so a later edit adding
    the owner tag must not enter Case 1 and force a second task — and it slips
    past the DM age guard because Case 1 is checked before it.
    """

    def setUp(self):
        _reset_ws_gate()
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.tasks.mkdir()
        db.seen_message_ids.clear()

    def tearDown(self):
        mg.write_state(_WS, mentions_enabled=False, until=None)
        db.seen_message_ids.clear()
        self._td.cleanup()

    def _dm_pair(self, age_sec):
        before, after = _FakeMsg("hello"), _FakeMsg("hello <@111222333>")
        before.mentions, after.mentions = [], [NS(id=111222333)]
        before.id = after.id = 880000001
        dm = mock.MagicMock(spec=discord.DMChannel)
        dm.id = 999000111
        dm.name = "dm"
        for m in (before, after):
            m.channel = dm
            m.created_at = NS(timestamp=lambda _t=time.time() - age_sec: _t)
        return before, after

    def _drive(self, age_sec):
        before, after = self._dm_pair(age_sec)
        fake_client = NS(user=object())

        async def _noop(*a, **k):
            return None

        buf = io.StringIO()
        with mock.patch.object(db, "client", fake_client), \
                mock.patch.object(db, "_observe_for_mod", _noop), \
                mock.patch.object(db, "TASKS_DIR", self.tasks), \
                mock.patch.object(db, "load_channel_config", lambda cid: None), \
                mock.patch.object(db, "load_allowed", lambda: {str(STRANGER)}), \
                contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            for coro in (db._handle_discord_message(before),
                         db.on_message_edit(before, after)):
                try:
                    asyncio.run(coro)
                except Exception:
                    pass
        return buf.getvalue(), sorted(p.name for p in self.tasks.glob("task-*.txt"))

    def test_an_old_dm_edited_to_add_the_owner_tag_is_not_ingested_twice(self):
        # Reviewer's control: main gives 1, the unscoped predicate gives 2.
        mg.write_state(_WS, mentions_enabled=True)
        out, written = self._drive(age_sec=600)
        self.assertLessEqual(len(written), 1,
                             f"a DM must not be re-queued by the gate, got {written}\n{out}")


class AnAddedBotMentionStillReprocesses(unittest.TestCase):
    """The explicit-bot branch of the predicate — the behaviour that predates
    the gate entirely, and the line the coverage gate reported uncovered."""

    def setUp(self):
        _reset_ws_gate()
        self._td = tempfile.TemporaryDirectory()
        self.tasks = Path(self._td.name) / "tasks"
        self.tasks.mkdir()
        db.seen_message_ids.clear()

    def tearDown(self):
        db.seen_message_ids.clear()
        self._td.cleanup()

    def test_gate_off_an_edit_adding_the_bot_mention_still_reprocesses(self):
        # Gate OFF on purpose: this path must not depend on the gate at all.
        mg.write_state(_WS, mentions_enabled=False)
        bot = NS(id=777000111)
        before, after = _FakeMsg("hello"), _FakeMsg("hello <@777000111>")
        before.mentions, after.mentions = [], [bot]
        before.id = after.id = 990000001
        seen = []
        fake_client = NS(user=bot)

        async def _capture(msg, force=False):
            seen.append((msg.id, force))

        buf = io.StringIO()
        with mock.patch.object(db, "client", fake_client), \
                mock.patch.object(db, "_handle_discord_message", _capture), \
                contextlib.redirect_stdout(buf):
            asyncio.run(db.on_message_edit(before, after))
        self.assertEqual(seen, [(990000001, True)], buf.getvalue())


if __name__ == "__main__":
    unittest.main()

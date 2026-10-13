#!/usr/bin/env python3
"""Owner questions cross a compaction as Pending-questions rows, never as carried prose.

The carrier was the compaction: open owner decisions rode the summary and session-state.md as
text and were re-printed each turn. Pinned here: the snapshot prints a count and a pointer, not
the questions; ask-owner files nothing that is already open, so a carried list can be filed
safely; the skill's hook tells the summarizer to leave questions out and, after a compaction,
tells the core to file any it carried.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SKILL = REPO / "skills" / "pending-questions"
HOOK = SKILL / "hooks" / "compact-carry.py"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SKILL / "scripts"))

CORE = {"SUTANDO_CORE_SESSION": "1"}
HELD = "Restart the AG2 Space connection for the live test once #5444 merges?"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _hold(ws: Path, ask_id: str, question: str) -> None:
    import pending_questions_outbox as pqo
    pqo.Outbox(ws).save({"ask_id": ask_id, "question": question, "context": None, "asked_at": 0,
                         "default_action": None, "reason": None, "options": [], "priority": "Medium"},
                        f"**Sent:** queued x via proactive-{ask_id}.txt at 2026-10-12T00:00:00Z")


def _run(argv, env=None):
    e = {k: v for k, v in os.environ.items() if k not in ("SUTANDO_CORE_SESSION", "SUTANDO_INSTANCE_ID")}
    e.update(env or {})
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, env=e, timeout=120)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.ws = Path(self._td.name)


class SnapshotCarriesACountNotTheQuestions(Base):
    def test_summary_counts_and_points_without_the_text(self):
        _hold(self.ws, "ask-held", HELD)
        r = _run([str(SRC / "pending_questions_reader.py"), "summary", "--workspace", str(self.ws)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 open in the owner's Pending questions database", r.stdout)
        self.assertIn("scripts/ask-owner.py", r.stdout)
        self.assertNotIn("AG2 Space connection", r.stdout)
        self.assertNotIn("ask-held", r.stdout)

    def test_an_unreadable_store_is_unknown_not_zero(self):
        reader = _load("pqr_carry", SRC / "pending_questions_reader.py")
        line = reader.summary_line({"waiting": [], "pending_close": [], "unavailable": True,
                                    "reason": "room unreachable", "store": None})
        self.assertIn("count UNKNOWN — room unreachable", line)
        self.assertNotIn("0 open", line)

    def test_session_handoff_prints_the_summary_not_the_list(self):
        src = (SRC / "session-handoff.sh").read_text()
        self.assertIn('pending_questions_reader.py" summary --workspace "$WORKSPACE_DIR"', src)
        self.assertNotIn('pending_questions_reader.py" list', src)


class AskOwnerFilesNoDuplicate(Base):
    def ask(self, question, *flags):
        return _run([str(REPO / "scripts" / "ask-owner.py"), question, "--urgency", "durable",
                     "--workspace", str(self.ws), *flags])

    def proactive(self):
        return sorted(p.name for p in (self.ws / "results").glob("proactive-*.txt"))

    def test_an_open_question_is_reported_and_not_filed_again(self):
        _hold(self.ws, "ask-held", HELD)
        r = self.ask("  restart the ag2 space connection for the live test once #5444   merges?", "--unless-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already open: [ask-held]", r.stdout)
        self.assertEqual(self.proactive(), [])
        reader = _load("pqr_carry2", SRC / "pending_questions_reader.py")
        self.assertEqual(len(reader.gather(self.ws, SKILL / "scripts" / "pending_questions_room_db.py")["waiting"]), 1)

    def test_a_new_question_is_still_filed(self):
        _hold(self.ws, "ask-held", HELD)
        r = self.ask("Merge #5430 once it has an approval?", "--unless-open")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("already open", r.stdout)
        self.assertIn("sent: queued", r.stdout)
        self.assertEqual(len(self.proactive()), 1)

    def test_without_the_flag_a_repeat_is_filed_as_before(self):
        _hold(self.ws, "ask-held", HELD)
        r = self.ask(HELD)
        self.assertNotIn("already open", r.stdout)
        self.assertEqual(len(self.proactive()), 1)

    def test_the_after_compaction_instruction_uses_the_flag(self):
        hook = _load("compact_carry_flag", HOOK)
        self.assertIn("--unless-open", hook.AFTER_COMPACT)

    def test_open_duplicate_is_none_when_the_store_cannot_be_read(self):
        reader = _load("pqr_carry3", SRC / "pending_questions_reader.py")

        class Down:
            @staticmethod
            def gather(ws):
                return {"waiting": [{"ask_id": "a", "title": HELD}], "done": None, "pending_close": [],
                        "unavailable": True, "reason": "down", "notes": []}
        self.assertIsNone(reader.open_duplicate(self.ws, HELD, Down))


class CompactionHook(unittest.TestCase):
    def setUp(self):
        self.h = _load("compact_carry", HOOK)

    def run_hook(self, payload, env):
        return _run([str(HOOK)], env) if payload is None else subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items()
                    if k not in ("SUTANDO_CORE_SESSION", "SUTANDO_INSTANCE_ID")}, **env}, timeout=30)

    def test_precompact_tells_the_summarizer_to_leave_owner_questions_out(self):
        r = self.run_hook({"hook_event_name": "PreCompact", "trigger": "auto"}, CORE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Do not write a 'Decisions for you' list", r.stdout)
        self.assertIn(self.h.TO_FILE, r.stdout)

    def test_after_a_compaction_the_core_is_told_to_file_what_was_carried(self):
        r = self.run_hook({"hook_event_name": "SessionStart", "source": "compact"}, CORE)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("python3 scripts/ask-owner.py", ctx["additionalContext"])
        self.assertIn(self.h.TO_FILE, ctx["additionalContext"])

    def test_silent_for_other_starts_guests_workers_and_garbage(self):
        for payload, env in (({"hook_event_name": "SessionStart", "source": "startup"}, CORE),
                             ({"hook_event_name": "SessionStart", "source": "resume"}, CORE),
                             ({"hook_event_name": "PreCompact"}, {}),
                             ({"hook_event_name": "PreCompact"}, {"SUTANDO_CORE_SESSION": "0"}),
                             ({"hook_event_name": "PreCompact"}, {**CORE, "SUTANDO_INSTANCE_ID": "w1"})):
            r = self.run_hook(payload, env)
            self.assertEqual((r.returncode, r.stdout), (0, ""), (payload, env))
        r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True,
                           env={**os.environ, **CORE}, timeout=30)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_skill_declares_both_hooks(self):
        from skill_hooks import discover
        mine = sorted(e for e, t, _, _ in discover(REPO) if t == HOOK.name)
        self.assertEqual(mine, ["PreCompact", "SessionStart"])


if __name__ == "__main__":
    unittest.main()

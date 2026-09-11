#!/usr/bin/env python3
"""The import-claude-context procedure starts the import in the greeting's turn.

2026-09-11, desktop v0.6.8-rc3 on engine 4b02fbaf: the consented import task sat in
`tasks/` for six minutes because SKILL.md step 0 read "answer it first … and only then
start" plus "can wait a minute", the task said `priority: low`, and nothing in the engine
re-delivers a task the core has already been told about. The core answered the hello,
finished the startup ceremony and went idle. These pin the sentences that close that:
step 0 must say the greeting and the import start share one turn; "When it runs" must
name the DM-message trigger the desktop now sends, the Settings sentence, and the legacy
task-file form; and the orphan check must carry the import exemption (step 3a) that keeps
a started import out of the recovery DM.

Run: python3 tests/import-claude-context-skill-triggers.test.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMPORT_SKILL = REPO / "skills" / "import-claude-context" / "SKILL.md"
ORPHAN_SKILL = REPO / "skills" / "task-orphan-check" / "SKILL.md"


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, f"section {heading!r} missing"
    return m.group(1)


class TestWhenItRuns(unittest.TestCase):
    def setUp(self):
        self.text = IMPORT_SKILL.read_text()
        self.when = section(self.text, "When it runs")

    def test_onboarding_trigger_is_the_dm_message(self):
        self.assertIn("import my Claude Code history", self.when)
        self.assertIn("consented on the onboarding card at <ISO>", self.when)
        self.assertIn("--run-kind onboarding", self.when)

    def test_settings_sentence_is_a_user_ask(self):
        self.assertIn("consented in Settings at <ISO>", self.when)
        self.assertIn("--run-kind user", self.when)

    def test_legacy_task_file_stays_documented(self):
        self.assertIn("channel_id: onboarding-wizard", self.when)
        self.assertIn("priority: low", self.when)
        self.assertRegex(self.when, r"[Ll]egacy")
        self.assertIn("defers nothing", self.when)


class TestStepZero(unittest.TestCase):
    def setUp(self):
        text = IMPORT_SKILL.read_text()
        m = re.search(r"^0\. \*\*(.*?)\*\*(.*?)(?=^1\. )", text, re.S | re.M)
        assert m, "step 0 missing"
        self.title, self.body = m.group(1), m.group(2)

    def test_same_turn(self):
        self.assertIn("same turn", self.title)
        self.assertIn("before ending the turn", self.body)
        self.assertIn("steps 1–4", self.body)

    def test_no_licence_to_wait(self):
        self.assertNotIn("can wait a minute", self.body)
        self.assertNotIn("only then start", self.body)
        self.assertIn("Never leave the import for a later turn", self.body)

    def test_names_the_mechanisms_that_do_not_rescue_a_deferred_task(self):
        self.assertIn("emits `TASK_FILE:` once", self.body)
        self.assertIn("`priority: low` is ordering metadata", self.body)
        self.assertIn("startup ceremony", self.body)


class TestOrphanCheckExemption(unittest.TestCase):
    def setUp(self):
        self.text = ORPHAN_SKILL.read_text()

    def test_step_two_runs_the_classifier(self):
        self.assertIn("skills/task-orphan-check/scripts/classify.py", self.text)
        self.assertTrue((REPO / "skills" / "task-orphan-check" / "scripts" / "classify.py").is_file())

    def test_step_3a_rules_are_stated(self):
        self.assertIn("Step 3a", self.text)
        for needle in ("channel_id: onboarding-wizard", "data/claude-import/status.json",
                       "IMPORT-RESUME", "IMPORT-STALLED", "IMPORT-UNBOUND", "1800 s", "3600 s",
                       "never archive it", "only by identity, never by timestamp",
                       "`status.task_id == <id>`", "`index.py --task-id <id>`",
                       "`/import-claude-context` as a standalone token",
                       "A path or a bare skill-name mention is **not** an intent",
                       "`done`, `staged`, `discarded`, `forgot`"):
            self.assertIn(needle, self.text, needle)
        for sentence in ("import my Claude history", "import my Claude Code history",
                         "read my Claude Code sessions", "bring my Claude context along"):
            self.assertIn(f'"{sentence}"', self.text, sentence)

    def test_never_orphan_window_is_named(self):
        self.assertIn("here is the window", self.text)
        self.assertIn("`indexed` write onward", self.text)

    def test_summary_counts_the_new_verdicts(self):
        self.assertIn("import-resume): I", self.text)
        self.assertIn("import-stalled): S", self.text)
        self.assertIn("import-unbound): U", self.text)
        self.assertIn("M+K+I+S+U+J", self.text)

    def test_recovery_dm_lists_unbound_and_stalled_imports_without_moving_them(self):
        self.assertIn("`unbound_imports`", self.text)
        self.assertIn("cannot be matched to this request", self.text)
        self.assertIn("Entries of `stalled_imports` and `unbound_imports` are **not** moved", self.text)


class TestIndexIsToldItsTask(unittest.TestCase):
    """Step 1 passes the task id to index.py, so every status.json the run writes can be
    matched back to the task by the orphan check."""

    def setUp(self):
        self.text = IMPORT_SKILL.read_text()

    def test_step_one_passes_the_task_id(self):
        m = re.search(r"^1\. \*\*Index\*\*(.*?)(?=^2\. )", self.text, re.S | re.M)
        assert m, "step 1 missing"
        self.assertIn("index.py --task-id <id> --json", m.group(1))
        self.assertIn("always pass it", m.group(1))
        self.assertIn("`run: {task_id, run_id, started_at}`", m.group(1))

    def test_flag_and_files_tables_name_the_run_identity(self):
        self.assertIn("| `--task-id <id>` | index |", self.text)
        self.assertIn("`state.json` `run` = `{task_id, run_id, started_at}`", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=1)

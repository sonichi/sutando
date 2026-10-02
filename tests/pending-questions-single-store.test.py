#!/usr/bin/env python3
"""Structural guard: the room database (through the declared adapter) is the ONLY store of
owner pending questions. No code under src/, scripts/, skills/ or packages/ opens the legacy
per-host pending-questions.md for current state — the one allowed reader is the transitional
ingest, and the only other mentions are legacy-file inventories in migration tooling. No
open-state path (waiting, list, count, resolve, remind) imports the ingest or any archive
reader, and the retired file readers are gone."""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("src", "scripts", "skills", "packages")
CODE = (".py", ".sh", ".ts", ".tsx", ".js", ".mjs", ".swift")
# The file named as a path or string literal; prose mentions in docstrings are not opens.
PATH_RE = re.compile(r"""(["'/=]|\bpath\s)pending-questions\.md""")
COMMENT_RE = re.compile(r"^\s*(#(?!!)|//|\*|/\*)")
# Files whose only mentions are inventories of a legacy file they move or tidy, never read.
INVENTORIES = {
    "skills/pending-questions/scripts/pending_questions_compat.py": "the transitional ingest (its docstring says when it goes)",
    "src/health-check.py": "workspace-root and hosts/ file inventories",
    "scripts/sutando-migrate.sh": "workspace migration of legacy files",
    "scripts/sync-workspace.sh": "vault migration of legacy files",
}
RETIRED = re.compile(r"\b(get_waiting_questions|parse_waiting|parse_pending_questions|answer_pending_question|"
                     r"FileStore|PQ_FILE|archived_ids|resync)\b")


def code_files():
    for root in ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            rel = p.relative_to(REPO).as_posix()
            if (p.is_file() and p.suffix in CODE and "__pycache__" not in rel and "node_modules" not in rel
                    and "test" not in p.name.lower() and "/tests/" not in rel and "/fixtures/" not in rel):
                yield rel, p


def code_lines(path):
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not COMMENT_RE.match(line):
            yield n, line


class TestSingleStore(unittest.TestCase):
    def test_no_code_opens_the_legacy_file_for_current_state(self):
        hits = {}
        for rel, p in code_files():
            for n, line in code_lines(p):
                if PATH_RE.search(line):
                    hits.setdefault(rel, []).append(f"{n}: {line.strip()}")
        unexpected = {k: v for k, v in hits.items() if k not in INVENTORIES}
        self.assertEqual(unexpected, {}, "a reader of the legacy file outside the transitional ingest")
        self.assertIn("skills/pending-questions/scripts/pending_questions_compat.py", hits, "the ingest still names the file it reads")

    def test_no_open_state_path_imports_the_ingest_or_an_archive_reader(self):
        importers = []
        for rel, p in code_files():
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(import|from) pending_questions_compat\b", text):
                importers.append(rel)
        self.assertEqual(importers, ["skills/pending-questions/scripts/pending_questions_store.py"],
                         "reconcile_pending() is the single call site")
        store = (REPO / "skills" / "pending-questions" / "scripts" / "pending_questions_store.py").read_text()
        self.assertEqual(store.count("ingest_legacy_file_entries("), 1)
        self.assertNotRegex(store, r"def (archive|archived_ids|legacy_entries)\b", "no archive reader in the store")
        for rel in ("src/pending_questions_reader.py", "src/pending_questions_outbox.py", "src/pending_questions_ask.py",
                    "src/check-pending-questions.py", "src/dashboard.py", "src/agent-api.py", "src/morning-briefing.py",
                    "src/friction-detector.py", "src/obsidian-mirror.py", "scripts/ask-owner.py",
                    "skills/pending-questions/scripts/pq.py", "skills/pending-questions/scripts/pending_questions_room_db.py",
                    "skills/pending-questions/scripts/pending_questions_remind.py"):
            self.assertNotRegex((REPO / rel).read_text(), r"pending_questions_compat|legacy_entries|pending_questions_md", rel)

    def test_the_retired_file_readers_are_gone(self):
        for rel, p in code_files():
            for n, line in code_lines(p):
                self.assertIsNone(RETIRED.search(line), f"{rel}:{n}: {line.strip()}")

    def test_every_core_reader_goes_through_the_reader_helper(self):
        for rel in ("src/dashboard.py", "src/agent-api.py", "src/morning-briefing.py", "src/friction-detector.py",
                    "src/obsidian-mirror.py", "src/check-pending-questions.py", "scripts/ask-owner.py"):
            self.assertIn("pending_questions_reader", (REPO / rel).read_text(), rel)
        self.assertIn("pending_questions_reader.py", (REPO / "src" / "session-handoff.sh").read_text())

    def test_core_keeps_only_the_contract_and_the_feature_lives_in_the_skill(self):
        """The layout the architecture rules ask for: core = reader (discovery by the manifest
        field), the one outbox-record writer, the provider-neutral queue, two thin entries; the
        store, replay, ingest, reminder and adapter are the skill's. Core names no skill."""
        src = REPO / "src"
        for gone in ("pending_questions_store.py", "pending_questions_compat.py"):
            self.assertFalse((src / gone).exists(), f"{gone} is feature policy; it belongs to the skill")
        skill = REPO / "skills" / "pending-questions" / "scripts"
        for there in ("pending_questions_store.py", "pending_questions_compat.py", "pending_questions_remind.py",
                      "pending_questions_room_db.py", "pq.py"):
            self.assertTrue((skill / there).exists(), there)
        for rel in ("src/pending_questions_reader.py", "src/pending_questions_outbox.py", "src/pending_questions_ask.py",
                    "src/check-pending-questions.py", "scripts/ask-owner.py"):
            text = (REPO / rel).read_text()
            # The manifest FIELD `pending_questions_store` is the contract core reads; the modules are not.
            self.assertNotRegex(text, r"room[-_]collab|room[-_]commons|pending_questions_room_db|skills/pending-questions|"
                                      r"pending_questions_(store|compat|remind)\.py|"
                                      r"(import|from) pending_questions_(store|compat|remind)\b", rel)
        self.assertLess(len((REPO / "src" / "check-pending-questions.py").read_text().splitlines()), 60, "a thin shim")

    def test_the_inventory_allowlist_is_still_needed(self):
        """A stale allow-list entry hides the next real reader; drop entries whose mentions are gone."""
        for rel in INVENTORIES:
            p = REPO / rel
            self.assertTrue(p.exists(), rel)
            self.assertTrue(any(PATH_RE.search(line) for _, line in code_lines(p)), f"{rel}: allow-list entry unused")


if __name__ == "__main__":
    unittest.main()

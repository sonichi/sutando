#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/finalize.py — summaries -> sinks.

Pins: memory file <= 2,000 bytes with one line per project; the MEMORY.md row
written only when memory-index-budget.py exits 0 (subprocess patched) and
skipped on exit 1 with the file kept; notes layout (frontmatter + header
line) and the dated `## Update` section on a changed roll-up; People payload
cap (25) / shape / >=2-citation floor; --forget removes exactly one project;
--purge-dumps; the vanilla-home memory-dir refusal.

Run: python3 tests/import-claude-context-finalize.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")

SLUG_A = "-Users-o-Projects-alpha"
SLUG_B = "-Users-o-Projects-beta"
U1, U2, U3 = "a1a1a1a1-0000-4000-8000-000000000001", "a2a2a2a2-0000-4000-8000-000000000002", "b3b3b3b3-0000-4000-8000-000000000003"


def _load():
    spec = importlib.util.spec_from_file_location("ici_finalize", SCRIPTS / "finalize.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _j(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def _cite(project, session, text="talked about the raise"):
    return {"project": project, "session": session, "quote_or_context": text}


def make_data(data: Path, ws: Path) -> None:
    _j(data / "index.json", {
        "generated_at": "2026-09-10T00:00:00Z", "root": "/Users/o/.claude/projects",
        "counts": {"sessions": 3, "projects": 2},
        "projects": {
            SLUG_A: {"slug": SLUG_A, "cwd": "/Users/o/Projects/alpha", "session_count": 2,
                     "first_ts": "2026-08-01T10:00:00Z", "last_ts": "2026-08-05T10:00:00Z",
                     "sessions": [
                         {"uuid": U1, "title": "Widget build", "first_ts": "2026-08-01T10:00:00Z",
                          "last_ts": "2026-08-01T12:00:00Z"},
                         {"uuid": U2, "title": "Widget polish", "first_ts": "2026-08-05T09:00:00Z",
                          "last_ts": "2026-08-05T10:00:00Z"}]},
            SLUG_B: {"slug": SLUG_B, "cwd": "/Users/o/Projects/beta", "session_count": 1,
                     "first_ts": "2026-07-20T08:00:00Z", "last_ts": "2026-07-20T09:00:00Z",
                     "sessions": [{"uuid": U3, "title": "Beta launch", "first_ts": "2026-07-20T08:00:00Z",
                                   "last_ts": "2026-07-20T09:00:00Z"}]},
        }})
    for slug, uuid in ((SLUG_A, U1), (SLUG_A, U2), (SLUG_B, U3)):
        _j(data / "summaries" / slug / f"{uuid}.json",
           {"session": uuid, "project": slug, "title": "t", "summary": "s"})
    _j(data / "summaries" / SLUG_A / f"{U2}.1.json", {"partial": True})   # ignored
    _j(data / "projects" / f"{SLUG_A}.json", {
        "project": SLUG_A, "name": "Alpha", "what_it_is": "A widget builder",
        "status": "active", "top_open_thread": "ship the widget", "sessions": [U1, U2],
        "note_markdown": "Alpha builds widgets.\n\nOpen: ship it."})
    _j(data / "projects" / f"{SLUG_B}.json", {
        "project": SLUG_B, "name": "Beta", "what_it_is": "A launch plan",
        "status": "paused", "top_open_thread": "", "sessions": [U3],
        "note_markdown": "Beta is a launch plan."})
    people = [
        {"name": "Ada Lovelace", "email": "ada@example.com", "company": "Analytical", "role": "CTO",
         "relationship": "investor", "citations": [_cite(SLUG_A, U1), _cite(SLUG_A, U2), _cite(SLUG_B, U3)]},
        {"name": "Only Alpha", "relationship": "colleague", "citations": [_cite(SLUG_A, U1), _cite(SLUG_A, U2)]},
        {"name": "One Citation", "relationship": "x", "citations": [_cite(SLUG_B, U3)]},
    ]
    for i in range(27):
        people.append({"name": f"Person {i:02d}", "relationship": "contact",
                       "citations": [_cite(SLUG_B, U3), _cite(SLUG_A, U1)]})
    _j(data / "entities.json", {"people": people, "companies": [
        {"name": "Analytical", "citations": [_cite(SLUG_A, U1)]}], "deals": [], "decisions": [],
        "open_threads": []})
    for slug in (SLUG_A, SLUG_B):
        d = data / "dumps" / slug
        d.mkdir(parents=True)
        (d / "x.1.txt").write_text("dump")
    (ws / "notes").mkdir(parents=True)


def _ok(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="✓ safe\n")


def _refuse(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="✗ REFUSE — drops 1 row\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        self.data = self.ws / "data" / "claude-import"
        self.mem = self.tmp / "memory"
        self.mem.mkdir()
        (self.mem / "MEMORY.md").write_text("- [Existing lesson](existing.md) — keep me\n")
        make_data(self.data, self.ws)

    def _finalize(self, runner=_ok, **kw):
        err = io.StringIO()
        with patch.object(self.m.subprocess, "run", side_effect=runner) as run, redirect_stderr(err):
            c = self.m.finalize(data_dir=self.data, ws=self.ws, memory_dir=self.mem,
                                run_kind=kw.get("run_kind", "onboarding"))
        return c, run, err.getvalue()


class TestMemorySinks(Base):
    def test_memory_file_and_row_on_budget_ok(self):
        c, run, log = self._finalize()
        mem = (self.mem / "claude_import.md").read_text()
        self.assertLessEqual(len(mem.encode()), 2000)
        self.assertIn("Imported 3 sessions / 2 projects on ", mem)
        self.assertIn("- Alpha — A widget builder; active; open: ship the widget", mem)
        self.assertIn("- Beta — A launch plan; paused", mem)
        self.assertIn("notes/claude-import/", mem)
        index = (self.mem / "MEMORY.md").read_text()
        self.assertIn("- [Existing lesson](existing.md) — keep me", index)
        self.assertEqual(index.count("](claude_import.md)"), 1)
        self.assertIn("- [Claude Code history import](claude_import.md) — 2 projects, open threads, people", index)
        args = run.call_args[0][0]
        self.assertIn("--adding", args)
        self.assertIn(str(self.mem / "MEMORY.md"), args)
        self.assertTrue(str(args[1]).endswith("memory-index-budget.py"))
        self.assertTrue(c["memory_row"])
        self.assertEqual((c["sessions"], c["summarized"], c["projects"]), (3, 3, 2))
        self.assertIn("row appended", log)
        # idempotent: a second finalize does not duplicate the row
        self._finalize()
        self.assertEqual((self.mem / "MEMORY.md").read_text().count("](claude_import.md)"), 1)

    def test_row_skipped_on_budget_refusal_file_kept(self):
        c, run, log = self._finalize(runner=_refuse)
        self.assertTrue((self.mem / "claude_import.md").is_file())
        index = (self.mem / "MEMORY.md").read_text()
        self.assertNotIn("claude_import.md", index)
        self.assertIn("keep me", index)
        self.assertFalse(c["memory_row"])
        self.assertIn("refused", log)
        status = json.loads((self.data / "status.json").read_text())
        self.assertEqual(status["phase"], "done")
        self.assertFalse(status["memory_row"])

    def test_missing_index_is_created_without_asking_the_budget(self):
        (self.mem / "MEMORY.md").unlink()
        c, run, _log = self._finalize()
        self.assertEqual(run.call_count, 0)
        self.assertEqual((self.mem / "MEMORY.md").read_text(),
                         "- [Claude Code history import](claude_import.md) — 2 projects, open threads, people\n")

    def test_memory_file_fits_many_long_projects(self):
        index_doc = {"counts": {"sessions": 500}, "projects": {}}
        rollups = {}
        for i in range(60):
            slug = f"-Users-o-p{i:02d}"
            index_doc["projects"][slug] = {"session_count": 60 - i}
            rollups[slug] = {"name": f"Project number {i:02d} with a long name",
                             "what_it_is": "what it is " * 12, "status": "active",
                             "top_open_thread": "open thread " * 12}
        text = self.m.render_memory_file(rollups, index_doc, 500)
        self.assertLessEqual(len(text.encode()), 2000)
        self.assertIn("Imported 500 sessions / 60 projects", text)
        self.assertIn("more projects in notes/claude-import/overview.md", text)
        self.assertIn("Project number 00", text)   # most sessions first

    def test_state_records_summarized_at(self):
        self._finalize()
        state = json.loads((self.data / "state.json").read_text())
        for slug, u in ((SLUG_A, U1), (SLUG_A, U2), (SLUG_B, U3)):
            self.assertIn("summarized_at", state["sessions"][f"{slug}/{u}"])
        self.assertNotIn(f"{SLUG_A}/{U2}.1", state["sessions"])


class TestNotes(Base):
    def test_layout_header_and_dated_update_section(self):
        self._finalize()
        ndir = self.ws / "notes" / "claude-import"
        note = (ndir / f"{SLUG_A}.md").read_text()
        self.assertTrue(note.startswith("---\ntitle: Claude Code history — Alpha\ndate: "))
        self.assertIn("tags: [imported, claude-code]", note)
        self.assertIn("*[imported, claude-code] — import-claude-context | 2026-08-01 → 2026-08-05 | onboarding*", note)
        self.assertIn("# Alpha\n", note)
        self.assertIn("Project dir: `/Users/o/Projects/alpha`", note)
        self.assertIn("Alpha builds widgets.", note)
        self.assertIn("## Sessions\n- 2026-08-01 — Widget build (a1a1a1a1)\n- 2026-08-05 — Widget polish (a2a2a2a2)", note)
        self.assertNotIn("## Update", note)
        overview = (ndir / "overview.md").read_text()
        self.assertIn("*[imported, claude-code] — import-claude-context | 2026-07-20 → 2026-08-05 | onboarding*", overview)
        self.assertIn(f"- [Alpha]({SLUG_A}.md) — A widget builder · active · 2 sessions (2026-08-01 → 2026-08-05) · open: ship the widget", overview)
        self.assertIn(f"- [Beta]({SLUG_B}.md)", overview)

        # same roll-up again: nothing appended
        c, _r, _l = self._finalize()
        self.assertEqual(c["notes_unchanged"], 2)
        self.assertNotIn("## Update", (ndir / f"{SLUG_A}.md").read_text())

        # changed roll-up: one dated update section
        p = self.data / "projects" / f"{SLUG_A}.json"
        doc = json.loads(p.read_text())
        doc["note_markdown"] = "Alpha shipped the widget."
        doc["top_open_thread"] = "write the launch post"
        p.write_text(json.dumps(doc))
        c, _r, _l = self._finalize()
        self.assertEqual((c["notes_updated"], c["notes_unchanged"]), (1, 1))
        note = (ndir / f"{SLUG_A}.md").read_text()
        self.assertEqual(note.count("## Update "), 1)
        self.assertIn(f"## Update {self.m.today()}\n\nAlpha shipped the widget.", note)
        self.assertIn("Alpha builds widgets.", note)   # history kept
        self.assertIn("open: write the launch post", (self.mem / "claude_import.md").read_text())


class TestPeople(Base):
    def test_payload_cap_floor_and_shape(self):
        index_doc = json.loads((self.data / "index.json").read_text())
        payloads = self.m.people_payloads(self.m.load_entities(self.data), index_doc)
        self.assertEqual(len(payloads), 25)
        names = [p["name"] for p in payloads]
        self.assertEqual(names[0], "Ada Lovelace")          # most citations first
        self.assertNotIn("One Citation", names)
        for p in payloads:
            self.assertEqual(set(p) - {"email", "identifiers"}, {"name", "doc", "source", "last_interaction_at"})
            self.assertEqual(p["source"], "claude-import")
            self.assertTrue(p["doc"].startswith(f"# {p['name']}\n"))
            for section in ("## Contact", "## Who they are", "## Your relationship",
                            "## Recent interactions", "## Claims and citations"):
                self.assertIn(section, p["doc"])
        ada = payloads[0]
        self.assertEqual(ada["email"], "ada@example.com")
        self.assertEqual(ada["identifiers"], {"emails": ["ada@example.com"]})
        self.assertEqual(ada["last_interaction_at"], "2026-08-05T10:00:00Z")
        self.assertIn("Claude Code session a1a1a1a1", ada["doc"])
        self.assertNotIn("email", payloads[1])

    def test_cli_people_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--people-json"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(buf.getvalue())), 25)


class TestForgetAndPurge(Base):
    def test_forget_removes_exactly_one_project(self):
        self._finalize()
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            r = self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=SLUG_A)
        ndir = self.ws / "notes" / "claude-import"
        self.assertFalse((ndir / f"{SLUG_A}.md").exists())
        self.assertTrue((ndir / f"{SLUG_B}.md").exists())
        self.assertFalse((self.data / "summaries" / SLUG_A).exists())
        self.assertTrue((self.data / "summaries" / SLUG_B / f"{U3}.json").exists())
        self.assertFalse((self.data / "projects" / f"{SLUG_A}.json").exists())
        self.assertTrue((self.data / "projects" / f"{SLUG_B}.json").exists())
        self.assertFalse((self.data / "dumps" / SLUG_A).exists())
        self.assertTrue((self.data / "dumps" / SLUG_B).exists())
        state = json.loads((self.data / "state.json").read_text())
        self.assertEqual([k for k in state["sessions"] if k.startswith(SLUG_A + "/")], [])
        self.assertIn(f"{SLUG_B}/{U3}", state["sessions"])
        self.assertNotIn(SLUG_A, state["projects"])
        ents = json.loads((self.data / "entities.json").read_text())
        names = {p["name"] for p in ents["people"]}
        self.assertNotIn("Only Alpha", names)
        self.assertIn("Ada Lovelace", names)
        ada = next(p for p in ents["people"] if p["name"] == "Ada Lovelace")
        self.assertEqual([c["project"] for c in ada["citations"]], [SLUG_B])
        self.assertEqual(ents["companies"], [])
        mem = (self.mem / "claude_import.md").read_text()
        self.assertNotIn("Alpha", mem)
        self.assertIn("- Beta —", mem)
        self.assertIn("Imported 3 sessions / 1 projects", mem)
        self.assertIn("](claude_import.md)", (self.mem / "MEMORY.md").read_text())
        overview = (ndir / "overview.md").read_text()
        self.assertNotIn("Alpha", overview)
        self.assertIn("[Beta]", overview)
        self.assertEqual((r["note"], r["rollup"], r["summaries"], r["state_sessions"]), (1, 1, 3, 2))
        self.assertEqual(r["entities_dropped"], 2)   # "Only Alpha" and the Alpha-only company

        # forgetting the last project retires the memory file and the row
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            r2 = self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=SLUG_B)
        self.assertFalse((self.mem / "claude_import.md").exists())
        self.assertFalse((ndir / "overview.md").exists())
        index = (self.mem / "MEMORY.md").read_text()
        self.assertNotIn("claude_import.md", index)
        self.assertIn("keep me", index)
        self.assertEqual(r2["memory_row_removed"], 1)

    def test_purge_dumps(self):
        n = self.m.purge_dumps(self.data)
        self.assertEqual(n, 2)
        self.assertFalse((self.data / "dumps").exists())
        self.assertEqual(self.m.purge_dumps(self.data), 0)

    def test_cli_forget_and_purge(self):
        self._finalize()
        buf = io.StringIO()
        # bare `--forget -Users-…`: real slugs start with "-" and must not be read as a flag
        with patch.object(self.m.subprocess, "run", side_effect=_ok), redirect_stdout(buf):
            rc = self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem),
                              "--forget", SLUG_B, "--purge-dumps", "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["note"], 1)
        self.assertEqual(doc["purged_files"], 1)   # SLUG_B's dump went with --forget; one left
        self.assertFalse((self.data / "dumps").exists())


class TestMemoryDirGuard(Base):
    def test_refuses_memory_dir_under_vanilla_home(self):
        fake_home = self.tmp / ".claude"
        with patch.object(self.m, "core_memory_dir", return_value=fake_home / "projects" / "x" / "memory"), \
                patch.object(self.m, "claude_home_path", return_value=fake_home):
            with self.assertRaises(SystemExit):
                self.m.resolve_memory_dir(None)
            self.assertEqual(self.m.resolve_memory_dir(str(self.mem)), self.mem.resolve())
        with patch.object(self.m, "core_memory_dir", return_value=self.mem), \
                patch.object(self.m, "claude_home_path", return_value=fake_home):
            self.assertEqual(self.m.resolve_memory_dir(None), self.mem)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

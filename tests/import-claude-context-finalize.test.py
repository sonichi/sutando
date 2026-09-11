#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/finalize.py — summaries -> sinks.

Pins: memory file <= 2,000 bytes with one line per project; the MEMORY.md row
written only when memory-index-budget.py exits 0 (subprocess patched) and
skipped on exit 1 with the file kept; notes layout (frontmatter + header
line) and the dated `## Update` section on a changed roll-up; People payload
cap (25) / shape / >=2-citation floor; --forget removes exactly one project
(its approved snapshot too), takes only a known slug or a unique part of one —
never a path, a symlink or an unknown value — and shrinks the approved People
export (`people.json`) to the citations still approved; --purge-dumps; the
vanilla-home memory-dir refusal.

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
            self.assertEqual(set(p) - {"email", "identifiers"}, {"name", "doc", "source", "last_interaction_at", "existing"})
            self.assertIsNone(p["existing"])          # no store listing: nobody is matched
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
        self.assertEqual(r["approved"], 1)
        self.assertFalse((self.data / "approved" / f"{SLUG_A}.json").exists())
        self.assertTrue((self.data / "approved" / f"{SLUG_B}.json").is_file())

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


def _tree(root: Path) -> list:
    """Every file under `root` with its size — the 'nothing was deleted' witness."""
    return sorted((str(p.relative_to(root)), p.lstat().st_size) for p in root.rglob("*") if not p.is_dir())


class TestForgetGuard(Base):
    """PR #4127 review: `--forget ../outside` deleted notes/outside.md and the
    rmtree joins allowed the same. The selector is now a known slug (or a unique
    part of one) and every target must resolve inside the import's own dirs."""

    def _forget(self, token):
        with patch.object(self.m.subprocess, "run", side_effect=_ok), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=token)
        code = cm.exception.code
        self.assertTrue(code and isinstance(code, str))      # a message: the process exits 1
        return code

    def test_path_shaped_unknown_and_ambiguous_values_delete_nothing(self):
        self._finalize()
        (self.ws / "notes" / "outside.md").write_text("mine")
        (self.tmp / "abs.md").write_text("mine")
        before = _tree(self.tmp)
        for token in ("../outside", "/abs", "a/b", "..", "", "   ", "~", "~/x", "a\\b", None, 7):
            self.assertIn("never a path", self._forget(token), repr(token))
        self.assertIn("no project matches 'zzz'", self._forget("zzz"))
        self.assertIn("matches 2 projects", self._forget("Projects"))
        for msg in (self._forget("../outside"), self._forget("zzz"), self._forget("Projects")):
            self.assertIn("nothing was deleted", msg)
        self.assertEqual(_tree(self.tmp), before)
        self.assertTrue((self.ws / "notes" / "outside.md").is_file())
        self.assertEqual(json.loads((self.data / "state.json").read_text())["projects"].keys(), {SLUG_A, SLUG_B})

    def test_cli_exit_is_nonzero_and_names_the_rule(self):
        self._finalize()
        (self.ws / "notes" / "outside.md").write_text("mine")
        before = _tree(self.tmp)
        err = io.StringIO()
        with patch.object(self.m.subprocess, "run", side_effect=_ok), redirect_stdout(io.StringIO()), redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--forget", "../outside", "--json"])
        self.assertIn("--forget takes a project slug", str(cm.exception.code))
        self.assertEqual(_tree(self.tmp), before)

    def test_symlinked_note_pointing_outside_is_refused_before_any_deletion(self):
        self._finalize()
        secret = self.tmp / "elsewhere" / "secret.md"
        secret.parent.mkdir()
        secret.write_text("keep")
        note = self.ws / "notes" / "claude-import" / f"{SLUG_A}.md"
        note.unlink()
        note.symlink_to(secret)
        before = _tree(self.tmp)
        msg = self._forget(SLUG_A)
        self.assertIn("symlink", msg)
        self.assertIn("nothing was deleted", msg)
        self.assertEqual(_tree(self.tmp), before)
        self.assertEqual(secret.read_text(), "keep")
        self.assertTrue(note.is_symlink())
        self.assertTrue((self.data / "summaries" / SLUG_A / f"{U1}.json").is_file())
        self.assertTrue((self.data / "projects" / f"{SLUG_A}.json").is_file())
        # the roll-up file as a symlink is refused the same way; a cross-drive compare cannot prove "inside"
        note.unlink()
        note.write_text("back")
        rollup = self.data / "projects" / f"{SLUG_A}.json"
        rollup.rename(self.tmp / "elsewhere" / "rollup.json")
        rollup.symlink_to(self.tmp / "elsewhere" / "rollup.json")
        self.assertIn("symlink", self._forget(SLUG_A))
        rollup.unlink()
        (self.tmp / "elsewhere" / "rollup.json").rename(rollup)
        with patch.object(self.m.os.path, "commonpath", side_effect=ValueError("drives")):
            self.assertIn("outside", self._forget(SLUG_A))
        self.assertTrue(rollup.is_file())

    def test_a_unique_part_of_a_slug_forgets_exactly_that_project(self):
        self._finalize()
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            r = self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug="alpha")
        self.assertEqual((r["note"], r["rollup"], r["approved"], r["summaries"]), (1, 1, 1, 3))
        self.assertFalse((self.ws / "notes" / "claude-import" / f"{SLUG_A}.md").exists())
        self.assertTrue((self.ws / "notes" / "claude-import" / f"{SLUG_B}.md").is_file())
        self.assertTrue((self.data / "projects" / f"{SLUG_B}.json").is_file())
        # the slug is known from the index alone (nothing staged or landed), or from the artefacts alone
        self.assertEqual(self.m.known_slugs(self.data), [SLUG_A, SLUG_B])    # the index row stays until re-indexed
        (self.data / "index.json").unlink()
        (self.data / "state.json").unlink()
        self.assertEqual(self.m.known_slugs(self.data), [SLUG_B])
        for token in ("-Users-o-Projects-beta", "beta", "BETA"):
            self.assertEqual(self.m.resolve_forget_slug(self.data, token), SLUG_B)
        self.assertTrue(self.m.canonical_slug("-Users-o-Projects-gtm"))
        self.assertFalse(self.m.canonical_slug("."))


class TestPeopleExportRevocation(Base):
    """PR #4127 review: after a commit, people.json (the approved export) was
    byte-identical after `--forget A` and still cited A. Now every forget/hold
    shrinks it to the citations still approved; it grows only with a commit."""

    def setUp(self):
        super().setUp()
        p = self.data / "entities.json"
        ents = json.loads(p.read_text())
        ents["people"].append({"name": "Beta Twice", "relationship": "advisor",
                               "citations": [_cite(SLUG_B, U3, "one"), _cite(SLUG_B, U3, "two")]})
        p.write_text(json.dumps(ents))

    def _export(self):
        path = self.data / "people.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def test_forget_drops_the_projects_citations_and_the_people_under_the_floor(self):
        self._finalize()
        names = [p["name"] for p in self._export()]
        self.assertEqual(len(names), 25)
        for name in ("Ada Lovelace", "Only Alpha", "Beta Twice"):
            self.assertIn(name, names)
        inputs = json.loads((self.data / "approved" / "people-inputs.json").read_text())
        self.assertEqual((inputs["projects"], inputs["known_people"]), ([SLUG_A, SLUG_B], None))
        self.assertEqual(oct(os.stat(self.data / "approved" / "people-inputs.json").st_mode & 0o777), "0o600")
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            r = self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=SLUG_A)
        self.assertEqual(r["people_revoked"], 24)
        export = self._export()
        self.assertEqual([p["name"] for p in export], ["Beta Twice"])        # Ada and the "Person NN"s fell to one citation
        text = json.dumps(export)
        for leak in (SLUG_A, U1[:8], U2[:8], "Only Alpha"):
            self.assertNotIn(leak, text)
        self.assertIn('"one" — Claude Code session b3b3b3b3', export[0]["doc"])
        inputs = json.loads((self.data / "approved" / "people-inputs.json").read_text())
        self.assertEqual(inputs["projects"], [SLUG_B])
        self.assertNotIn(SLUG_A, json.dumps(inputs))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--people-json"])
        self.assertEqual(json.loads(buf.getvalue()), export)                # the approved copy is the revoked one
        # the last project goes: no export is left at all
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            r2 = self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=SLUG_B)
        self.assertEqual(r2["people_revoked"], 1)
        self.assertIsNone(self._export())
        self.assertFalse((self.data / "approved" / "people-inputs.json").exists())
        self.assertEqual(self.m.refresh_people_export(self.data), 0)

    def test_an_export_without_its_inputs_is_retired_not_recomputed(self):
        self._finalize()
        (self.data / "approved" / "people-inputs.json").unlink()
        self.assertEqual(self.m.refresh_people_export(self.data), 25)
        self.assertIsNone(self._export())
        self.assertEqual(self.m._entity_lists("junk"), {k: [] for k in self.m.ENTITY_LISTS})


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



class TestHelpers(Base):
    def test_loaders_on_an_empty_data_dir(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(self.m.load_summaries(empty), {})
        self.assertEqual(self.m.load_rollups(empty), {})
        self.assertEqual(self.m.load_entities(empty), {k: [] for k in self.m.ENTITY_LISTS})

    def test_fingerprint_skips_an_unreadable_input(self):
        full = self.m.inputs_fingerprint(self.data)
        with patch.object(self.m.Path, "read_bytes", side_effect=OSError("denied")):
            partial = self.m.inputs_fingerprint(self.data)
        self.assertNotEqual(full, partial)
        self.assertRegex(partial, r"^[0-9a-f]{64}$")

    def test_meta_lookups_and_display_name(self):
        index_doc = json.loads((self.data / "index.json").read_text())
        self.assertEqual(self.m.session_meta(index_doc, SLUG_A, "nope"), {})
        self.assertEqual(self.m.session_meta(index_doc, "-no-such", U1), {})
        self.assertEqual(self.m.display_name(SLUG_A, {"name": " Alpha "}, index_doc), "Alpha")
        self.assertEqual(self.m.display_name(SLUG_A, {}, index_doc), "alpha")
        self.assertEqual(self.m.display_name("-Users-o-Projects-gamma", {"name": ""}, index_doc), "gamma")
        self.assertEqual(self.m.display_name("-", {}, {}), "-")
        self.assertEqual(self.m.display_name("-Users-o-x", {}, {"projects": {"-Users-o-x": {"cwd": "/"}}}), "x")

    def test_session_total_without_index_counts(self):
        summaries = {(SLUG_A, U1): {}, (SLUG_B, U3): {}}
        self.assertEqual(self.m.session_total({}, summaries, [SLUG_A, SLUG_B], [SLUG_A, SLUG_B]), 2)
        self.assertEqual(self.m.session_total({"counts": {"sessions": 5}}, summaries, [SLUG_A], [SLUG_A, SLUG_B]), 1)

    def test_memory_row_helpers(self):
        idx = self.mem / "MEMORY.md"
        idx.write_text("- [Existing lesson](existing.md) — keep me")   # no trailing newline
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            ok, _reason = self.m.guard_memory_row(self.mem, self.m.memory_row(2))
        self.assertTrue(ok)
        self.assertEqual(idx.read_text(), "- [Existing lesson](existing.md) — keep me\n" + self.m.memory_row(2) + "\n")
        self.assertTrue(self.m.remove_memory_row(self.mem))
        self.assertFalse(self.m.remove_memory_row(self.mem))
        idx.unlink()
        self.assertFalse(self.m.remove_memory_row(self.mem))

    def test_memory_dir_guard_survives_a_cross_drive_compare(self):
        with patch.object(self.m, "core_memory_dir", return_value=self.mem), \
                patch.object(self.m, "claude_home_path", return_value=self.tmp / ".claude"), \
                patch.object(self.m.os.path, "commonpath", side_effect=ValueError("drives")):
            self.assertEqual(self.m.resolve_memory_dir(None), self.mem)

    def test_note_rendering_edges(self):
        index_doc = json.loads((self.data / "index.json").read_text())
        summaries = self.m.load_summaries(self.data)
        rows = self.m._sessions_list(SLUG_A, {"sessions": [U1, 7, None]}, index_doc, summaries)
        self.assertEqual(rows, f"- 2026-08-01 — Widget build ({U1[:8]})")
        ndir = self.tmp / "notes"
        ndir.mkdir()
        state = {"sessions": {}, "projects": {}}
        rollup = json.loads((self.data / "projects" / f"{SLUG_A}.json").read_text())
        self.assertEqual(self.m.write_project_note(ndir, SLUG_A, rollup, index_doc, summaries, state, "user"), "created")
        self.assertIn("# Alpha\n", (ndir / f"{SLUG_A}.md").read_text())
        self.assertEqual(self.m.write_project_note(ndir, SLUG_A, rollup, index_doc, summaries, state, "user"), "unchanged")

    def test_review_line_helpers(self):
        rollup = {"top_open_thread": "ship", "open_threads": [{"thread": "t1"}, {"what": "t2"}, "t3", 5],
                  "key_decisions": ["plain decision", {"decision": "d2", "why": "w2"}, {"decision": "d3"}]}
        ents = {"open_threads": [{"project": SLUG_A, "thread": "t4", "owner_action": "call"},
                                 {"project": SLUG_B, "thread": "x"}, "junk"],
                "decisions": [{"project": SLUG_A, "decision": "d4"}]}
        self.assertEqual(self.m._thread_lines(SLUG_A, rollup, ents), ["ship", "t1", "t2", "t3", "t4 (you: call)"])
        self.assertEqual(self.m._decision_lines(SLUG_A, rollup, ents), ["plain decision", "d2 — because w2", "d3", "d4"])
        many = [f"item {i}" for i in range(self.m.REVIEW_MAX_ITEMS + 3)]
        self.assertIn("- …and 3 more\n", self.m._bullets("Threads", many))
        self.assertEqual(self.m._bullets("Threads", []), "Threads: none recorded.\n")
        self.assertEqual(self.m._paragraph({"summary": "S"}), "S")
        self.assertEqual(self.m._paragraph({"note_markdown": "Para one.\n\nPara two."}), "Para one.")
        self.assertEqual(self.m._paragraph({}), "(no roll-up text)")

    def test_merge_helpers_tolerate_junk_and_union_fields(self):
        self.assertEqual(self.m._norm_name(None), "")
        cite = _cite(SLUG_A, U1)
        self.assertEqual(self.m._union_citations([{"citations": ["junk", cite, dict(cite)]}]), [cite])
        people = [
            {"name": "Ada Lovelace", "email": "ada@example.com",
             "identifiers": {"handles": ["@ada"], "x": "ada"}, "citations": []},
            {"name": "Ada", "email": "ada@example.com", "company": "Analytical", "relationship": "investor",
             "identifiers": {"handles": ["@ada", "@lovelace"], "x": ""}, "citations": []},
        ]
        merged, n = self.m.merge_people(people)
        self.assertEqual(n, 1)
        self.assertEqual((merged[0]["name"], merged[0]["company"], merged[0]["relationship"]),
                         ("Ada Lovelace", "Analytical", "investor"))
        self.assertEqual(merged[0]["identifiers"],
                         {"handles": ["@ada", "@lovelace"], "x": "ada", "emails": ["ada@example.com"]})
        companies, n = self.m.merge_companies(["junk", {"name": "Acme"}, {"name": "acme", "what": "widgets"}])
        self.assertEqual((n, len(companies), companies[0]["what"]), (1, 1, "widgets"))

    def test_store_name_key_and_known_people_loader(self):
        self.assertEqual(self.m.store_name_key("  Zoë  Ångström (zoe@x.example) "), "zoe angstrom")
        self.assertEqual(self.m.store_name_key(None), "")
        self.assertIsNone(self.m.load_known_people(None))
        self.assertEqual(self.m.load_known_people([{"name": "A"}, "junk", 3]), [{"name": "A"}])
        self.assertEqual(self.m.load_known_people({"people": [{"name": "B"}]}), [{"name": "B"}])
        self.assertEqual(self.m.load_known_people({"name": "C"}), [{"name": "C"}])
        path = self.tmp / "known.json"
        path.write_text(json.dumps([{"name": "D"}]))
        self.assertEqual(self.m.load_known_people(path), [{"name": "D"}])
        path.write_text('"a string"')
        with self.assertRaises(SystemExit):
            self.m.load_known_people(str(path))
        self.assertEqual(self.m.match_known({"name": "D"}, None), (None, []))
        self.assertEqual(self.m.match_known({"name": "Nobody"}, [{"name": "D"}]), (None, []))

    def test_people_doc_merge_cases(self):
        merge = self.m.people_doc_merge
        section = "## Imported from Claude Code (2026-02-02)\n\n- a\n"
        self.assertEqual(merge("", section), section)
        self.assertEqual(merge("# Name\n\n## Contact\n- x\n", section), "# Name\n\n## Contact\n- x\n\n" + section)
        # the same heading in the middle is replaced up to the next heading, at the end without a trailer
        doc = "# Name\n\n## Imported from Claude Code (2026-02-02)\n\n- old\n\n# Appendix\ntail\n"
        self.assertEqual(merge(doc, section), "# Name\n\n## Imported from Claude Code (2026-02-02)\n\n- a\n\n# Appendix\ntail\n")
        doc = "# Name\n\n## Imported from Claude Code (2026-02-02)\n\n- old\n"
        self.assertEqual(merge(doc, section), "# Name\n\n## Imported from Claude Code (2026-02-02)\n\n- a\n")
        self.assertEqual(merge(doc, "no heading here\n"), doc.rstrip("\n") + "\n\nno heading here\n")
        self.assertEqual(merge(merge(doc, section), section), merge(doc, section))

    def test_held_helpers(self):
        self.assertEqual(self.m.held_reason({"personal_reason": "one two three four five six seven"}), "one two three four five six")
        self.assertEqual(self.m.held_reason({}), "personal")
        state = {"sessions": {f"{SLUG_A}/{U1}": {"personal_override": "hold"},
                              f"{SLUG_A}/{U2}": {"personal_override": "include"}}, "projects": {}}
        summaries = {(SLUG_A, U1): {}, (SLUG_A, U2): {"personal": True}, (SLUG_B, U3): {"personal": True, "personal_reason": "x"}}
        held = self.m.held_sessions(summaries, state)
        self.assertEqual(held, {(SLUG_A, U1): "held by you", (SLUG_B, U3): "x"})
        self.assertEqual(self.m.excluded_by_slug({"sessions": {f"{SLUG_B}/z": {"skipped_empty": True}}}, held),
                         {SLUG_A: 1, SLUG_B: 2})
        self.assertEqual(self.m.session_day({}, {(SLUG_A, U1): {"date_range": ["2026-03-03T10:00:00Z", ""]}}, SLUG_A, U1), "2026-03-03")
        self.assertEqual(self.m.session_day({}, {}, SLUG_A, U1), "?")
        self.assertEqual(self.m.drop_held_citations({"people": [1]}, {}), {"people": [1]})
        ents, n_c, n_d = self.m.strip_citations({"people": ["junk", {"name": "x", "citations": ["junk", _cite(SLUG_A, U1)]},
                                                            {"name": "y"}]}, lambda c: True)
        self.assertEqual((n_c, n_d, ents["people"]), (1, 1, [{"name": "y", "citations": []}]))
        rows = self.m.held_rows({}, summaries, held)
        self.assertEqual([r["session"] for r in rows], [U1, U3])
        self.assertEqual(self.m.held_rows({}, summaries, held, {SLUG_B}), [rows[1]])   # filtered to one project
        section = self.m._held_section([{"session": U1, "date": "2026-03-03", "reason": "a"},
                                        {"session": U2, "date": "2026-03-03", "reason": "b"}])
        self.assertIn(f"2026-03-03 · a (`{U1[:8]}`), 2026-03-03 · b (`{U2[:8]}`)", section)

    def test_stale_rollup_rules(self):
        index_doc = json.loads((self.data / "index.json").read_text())
        summaries = self.m.load_summaries(self.data)
        rollups = self.m.load_rollups(self.data)
        self.assertEqual(self.m.stale_rollups(rollups, summaries, {}, {"sessions": {}}, index_doc), {})
        # no `sessions` list at all: only an owner-included session makes it stale
        bare = {SLUG_A: {"name": "Alpha"}}
        self.assertEqual(self.m.stale_rollups(bare, summaries, {(SLUG_A, U1): "x"}, {"sessions": {}}, index_doc), {})
        state = {"sessions": {f"{SLUG_A}/{U1}": {"personal_override": "include"}}}
        self.assertEqual(self.m.stale_rollups(bare, summaries, {}, state, index_doc), {SLUG_A: "written without an included session"})
        # a listed uuid the index knows but no summary backs: forgotten — unless extract skipped it as empty
        del summaries[(SLUG_A, U2)]
        self.assertEqual(self.m.stale_rollups(rollups, summaries, {}, {"sessions": {}}, index_doc)[SLUG_A], "lists a forgotten session")
        skipped = {"sessions": {f"{SLUG_A}/{U2}": {"skipped_empty": True}}}
        self.assertNotIn(SLUG_A, self.m.stale_rollups(rollups, summaries, {}, skipped, index_doc))
        rollups[SLUG_A]["sessions"] = [U1, "not-in-the-index", 7]
        self.assertEqual(self.m.stale_rollups(rollups, summaries, {}, {"sessions": {}}, index_doc), {})
        stub = self.m.redact_rollup(rollups[SLUG_A])
        self.assertEqual((stub["name"], stub["what_it_is"], stub["stale_rollup"]), ("Alpha", "(roll-up pending regeneration)", True))
        self.assertNotIn("widget", json.dumps(stub).lower())
        self.assertEqual(self.m._note_body(stub), f"_{self.m.STALE_ROLLUP_TEXT}_")
        self.assertEqual(self.m._paragraph(stub), self.m.STALE_ROLLUP_TEXT)
        self.assertIn("(roll-up pending regeneration)", self.m._project_line(SLUG_A, stub, index_doc))

    def test_resolve_session_and_pool(self):
        index_doc = json.loads((self.data / "index.json").read_text())
        summaries = self.m.load_summaries(self.data)
        summaries[(SLUG_B, "ffffffff-0000-4000-8000-000000000009")] = {}       # a summary the index lost
        pool = self.m.session_pool(index_doc, summaries)
        self.assertEqual(len(pool), 4)
        self.assertEqual(self.m.resolve_session(pool, U1, index_doc, summaries), (SLUG_A, U1))
        self.assertEqual(self.m.resolve_session(pool, "FFFFFFFF", index_doc, summaries)[1], "ffffffff-0000-4000-8000-000000000009")
        self.assertEqual(self.m.resolve_session(pool, "2026-07-20", index_doc, summaries), (SLUG_B, U3))
        with self.assertRaises(SystemExit):
            self.m.resolve_session(pool, "2026-07-21", index_doc, summaries)
        with self.assertRaises(SystemExit):
            self.m.resolve_session(pool, None, index_doc, summaries)

    def test_review_people_lines_when_nobody_is_there(self):
        text = self.m._people_section([], [], 0, {}, True)
        self.assertIn("Already in your People store — will gain new interactions (0):\n- none\n", text)
        self.assertIn("New — will be added (0):\n- none\n", text)
        self.assertEqual(self.m._n(1, "citation"), "1 citation")

    def test_forget_drops_junk_entity_items(self):
        ents = json.loads((self.data / "entities.json").read_text())
        ents["people"].insert(0, "junk")
        (self.data / "entities.json").write_text(json.dumps(ents))
        with patch.object(self.m.subprocess, "run", side_effect=_ok):
            self.m.forget(data_dir=self.data, ws=self.ws, memory_dir=self.mem, slug=SLUG_B)
        people = json.loads((self.data / "entities.json").read_text())["people"]
        self.assertTrue(all(isinstance(p, dict) for p in people))
        self.assertNotIn("junk", people)


class TestPublishedPeopleMatchTheDigest(Base):
    """PR #4127 review: what `--commit` publishes to people.json must be exactly what
    the digest showed. A person cited twice by a landed project and once by the staged
    one crosses the two-citation floor only through the union; `--stage` now previews
    the post-union field change and a full `--commit` publishes the staged set verbatim.
    `published_field_diffs` lists a person only when the published fields differ."""

    def _run(self, *args):
        out = io.StringIO()
        with patch.object(self.m.subprocess, "run", side_effect=_ok), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--json", *args])

    def _export(self):
        return json.loads((self.data / "people.json").read_text())

    def test_diffs_list_only_changed_fields(self):
        pub = [({"name": "Ada Lovelace", "email": "ada@example.com", "role": "Principal",
                 "company": "Analytical"}, [1, 2, 3], None),
               ({"name": "Grace Hopper", "email": "g@h.example", "role": "Admiral"}, [1, 2], None),
               ({"name": "Grace B. Hopper", "email": "g@h.example", "role": "Admiral",
                 "identifiers": {"emails": ["g@h.example", "grace@navy.example"]}}, [1, 2], None),
               ({"name": "New Person", "role": "advisor"}, [1, 2], None)]
        prev = {"people": [
            {"name": "Ada Lovelace", "email": "ada@example.com", "role": "CTO", "company": "Analytical"},
            {"name": "Grace Hopper", "email": "g@h.example", "role": "Admiral"}]}
        rows = self.m.published_field_diffs(pub, prev)   # Grace(1) unchanged, New absent
        self.assertEqual(rows, [
            ("Ada Lovelace", ["role: CTO → Principal"], 3),
            ("Grace B. Hopper", ["name: Grace Hopper → Grace B. Hopper", "+1 email"], 2)])

    def test_shared_person_role_change_is_previewed_and_published_verbatim(self):
        self._run("--stage", "--projects", "alpha")
        self._run("--commit")
        self.assertIn("Role: CTO", next(p for p in self._export() if p["name"] == "Ada Lovelace")["doc"])
        ents = json.loads((self.data / "entities.json").read_text())
        ents["people"][0]["role"] = "Principal Engineer"     # people[0] is Ada in the fixture
        (self.data / "entities.json").write_text(json.dumps(ents))
        self._run("--stage", "--projects", "beta")
        staged = self.data / "staged"
        self.assertIn("Ada Lovelace — role: CTO → Principal Engineer", (staged / "review.md").read_text())
        staged_ada = next(p for p in json.loads((staged / "people.json").read_text())
                          if p["name"] == "Ada Lovelace")
        self.assertIn("Role: Principal Engineer", staged_ada["doc"])
        self._run("--commit")
        published_ada = next(p for p in self._export() if p["name"] == "Ada Lovelace")
        self.assertEqual(published_ada["doc"], staged_ada["doc"])
        self.assertIn("Role: Principal Engineer", published_ada["doc"])
        self.assertNotIn("Role: CTO", published_ada["doc"])

    def _set_ada_role(self, role):
        ents = json.loads((self.data / "entities.json").read_text())
        ents["people"][0]["role"] = role                          # people[0] is Ada in the fixture
        (self.data / "entities.json").write_text(json.dumps(ents))

    def test_same_prefix_role_change_is_shown_and_published(self):
        # PR #4127 review (P2): two roles sharing a 60-char prefix previewed alike, so the digest
        # showed no update row while the published 80-char role carried the changed suffix.
        prefix = "Senior director of international engineering and operations, "
        self._set_ada_role(prefix + "APPROVED")
        self._run("--stage", "--projects", "alpha")
        self._run("--commit")
        self.assertIn(f"Role: {prefix}APPROVED", next(p for p in self._export() if p["name"] == "Ada Lovelace")["doc"])
        self._set_ada_role(prefix + "UNREVIEWED")
        self._run("--stage", "--projects", "beta")
        staged = self.data / "staged"
        review = (staged / "review.md").read_text()
        self.assertIn("Updates to people already in your export (1)", review)
        self.assertIn(f"- Ada Lovelace — role: {prefix}APPROVED → {prefix}UNREVIEWED (now 3 citations)", review)
        staged_ada = next(p for p in json.loads((staged / "people.json").read_text()) if p["name"] == "Ada Lovelace")
        self.assertIn(f"Role: {prefix}UNREVIEWED", staged_ada["doc"])
        self._run("--commit")
        published_ada = next(p for p in self._export() if p["name"] == "Ada Lovelace")
        self.assertEqual(published_ada["doc"], staged_ada["doc"])
        self.assertIn(f"Role: {prefix}UNREVIEWED", published_ada["doc"])
        self.assertNotIn("APPROVED", published_ada["doc"])

    def test_field_diffs_compare_the_published_values_and_render_an_observable_delta(self):
        m = self.m
        self.assertEqual(m.field_delta("CTO", "Principal"), "CTO → Principal")
        self.assertEqual(m.field_delta("", "CTO"), "— → CTO")
        self.assertEqual(m.field_delta("CTO", None), "CTO → —")
        long = "Head of platform engineering for the northern, western and central regions of the group, "
        self.assertGreater(len(long + "reporting to the board, APPROVED"), m.DIFF_FULL_VALUE)
        # a long shared prefix is elided to the last word start; both tails stay visible
        self.assertEqual(m.field_delta(long + "reporting to the board, APPROVED", long + "reporting to the board, UNREVIEWED"),
                         "…APPROVED → …UNREVIEWED")
        # one value extends the other: the shared last word is kept so neither tail is empty
        self.assertEqual(m.field_delta(long + "reporting to the board", long + "reporting to the board (interim)"),
                         "…board → …board (interim)")
        # nothing shared before the first space: both values whole, however long
        self.assertEqual(m.field_delta("A" * 130, "B" * 130), "A" * 130 + " → " + "B" * 130)
        self.assertEqual(m.field_delta("x " + "A" * 130, "x " + "B" * 130), "…" + "A" * 130 + " → …" + "B" * 130)
        # published_field_diffs compares what the dossier prints: role/company at 80, relationship at 200
        role80 = "R" * 78 + " tail"                                     # 83 chars: cut at 80 in the dossier
        self.assertEqual(m.published_field({"role": role80}, "role"), "R" * 78 + "…")
        published = [({"name": "Ada Lovelace", "email": "ada@example.com", "role": role80 + " changed"}, [1, 2], None),
                     ({"name": "Grace Hopper", "email": "g@h.example", "relationship": "x" * 150 + " new"}, [1, 2], None)]
        prev = {"people": [{"name": "Ada Lovelace", "email": "ada@example.com", "role": role80},
                           {"name": "Grace Hopper", "email": "g@h.example", "relationship": "x" * 150 + " old"}]}
        rows = m.published_field_diffs(published, prev)
        self.assertEqual(rows, [("Grace Hopper", ["relationship: …old → …new"], 2)])   # Ada: same published role
        # the section never suppresses a row: two values that only differ past a preview cut still get one
        published = [({"name": "Ada Lovelace", "email": "ada@example.com", "role": "S" * 60 + " UNREVIEWED"}, [1, 2, 3], None)]
        prev = {"people": [{"name": "Ada Lovelace", "email": "ada@example.com", "role": "S" * 60 + " APPROVED"}]}
        rows = m.published_field_diffs(published, prev)
        self.assertEqual(rows, [("Ada Lovelace", ["role: " + "S" * 60 + " APPROVED → " + "S" * 60 + " UNREVIEWED"], 3)])
        self.assertIn("- Ada Lovelace — role: " + "S" * 60 + " APPROVED → " + "S" * 60 + " UNREVIEWED (now 3 citations)\n",
                      m._people_diff_section(rows))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

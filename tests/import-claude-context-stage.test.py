#!/usr/bin/env python3
"""Tests for the review-and-approve gate in skills/import-claude-context/scripts/finalize.py.

Pins: a bare run (neither --stage nor --commit) stages under <data-dir>/staged/
and leaves memory_dir(), <workspace>/notes and MEMORY.md untouched; review.md
names every project with its threads and decisions, the people it would add
(with citation counts) and ends with the reply footer; --commit moves the
staged set into the sinks with the memory cap and the budget guard (subprocess
patched) and sets phase `done`; --commit --projects lands one project and keeps
the other staged (people restricted to what landed); --discard --projects drops
one; a stale staged set, an empty one and an ambiguous selector are refused;
--forget re-renders a pending review; the deterministic people/company merge
(shared email, first name -> its unique full name, never two multi-token
names, companies by name) and its `people_merged` count in the JSON.

Run: python3 tests/import-claude-context-stage.test.py
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")

# The fixture (two projects, three sessions, 30 people) and the patched budget
# runners are the finalize test's; the gate is tested on the same data.
_spec = importlib.util.spec_from_file_location("ici_finalize_test", REPO / "tests" / "import-claude-context-finalize.test.py")
_fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fx)
make_data, _ok, _refuse, _cite = _fx.make_data, _fx._ok, _fx._refuse, _fx._cite
SLUG_A, SLUG_B, U1, U2, U3 = _fx.SLUG_A, _fx.SLUG_B, _fx.U1, _fx.U2, _fx.U3

FOOTER = ("Reply 'bring it in' to save this to your Sutando, 'bring in <slug>' "
          "for one project, or 'forget <slug>' to drop one.")


def _load():
    spec = importlib.util.spec_from_file_location("ici_finalize", SCRIPTS / "finalize.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
        # a roll-up summary + a decision so the digest has a paragraph and a decision to show
        p = self.data / "projects" / f"{SLUG_A}.json"
        doc = json.loads(p.read_text())
        doc["summary"] = "Alpha is where the owner built the widget builder across two sessions."
        doc["key_decisions"] = [{"decision": "write the core in Rust", "why": "startup time"}]
        doc["open_threads"] = ["ship the widget", "write the launch post"]
        p.write_text(json.dumps(doc))
        self.staged = self.data / "staged"
        self.ndir = self.ws / "notes" / "claude-import"

    def _run(self, *args, runner=_ok):
        """finalize.py <args> with the budget script patched; returns (rc, stdout JSON or text, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        argv = ["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--json", *args]
        with patch.object(self.m.subprocess, "run", side_effect=runner), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(argv)
        text = out.getvalue()
        try:
            return rc, json.loads(text), err.getvalue()
        except ValueError:
            return rc, text, err.getvalue()

    def _refused(self, *args, runner=_ok):
        with patch.object(self.m.subprocess, "run", side_effect=runner), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.m.main(["--workspace", str(self.ws), "--memory-dir", str(self.mem), "--json", *args])
        return str(cm.exception.code)

    def assertSinksUntouched(self):
        self.assertFalse((self.mem / "claude_import.md").exists())
        self.assertEqual((self.mem / "MEMORY.md").read_text(), "- [Existing lesson](existing.md) — keep me\n")
        self.assertFalse(self.ndir.exists())
        self.assertEqual(sorted(p.name for p in (self.ws / "notes").iterdir()), [])

    def _status(self):
        return json.loads((self.data / "status.json").read_text())

    def _manifest(self):
        return json.loads((self.staged / "manifest.json").read_text())


class TestStage(Base):
    def test_default_run_stages_and_touches_no_sink(self):
        rc, c, _err = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual((c["projects"], c["people"], c["sessions"], c["summarized"]), (2, 25, 3, 3))
        self.assertEqual((c["notes_created"], c["notes_updated"], c["notes_unchanged"]), (2, 0, 0))
        self.assertEqual((c["people_merged"], c["companies_merged"]), (0, 0))
        self.assertLessEqual(c["memory_bytes"], 2000)
        for rel in ("memory/claude_import.md", f"notes/claude-import/{SLUG_A}.md",
                    f"notes/claude-import/{SLUG_B}.md", "notes/claude-import/overview.md",
                    "people.json", "review.md", "manifest.json"):
            self.assertTrue((self.staged / rel).is_file(), rel)
        self.assertEqual(stat.S_IMODE(self.staged.stat().st_mode), 0o700)
        self.assertSinksUntouched()
        self.assertFalse((self.data / "state.json").exists())   # summarized_at is a commit's job
        self.assertEqual(self._status()["phase"], "staged")
        self.assertEqual(self._status()["projects"], 2)
        mem = (self.staged / "memory" / "claude_import.md").read_text()
        self.assertLessEqual(len(mem.encode()), 2000)
        self.assertIn("- Alpha — A widget builder; active; open: ship the widget", mem)
        note = (self.staged / "notes" / "claude-import" / f"{SLUG_A}.md").read_text()
        self.assertTrue(note.startswith("---\ntitle: Claude Code history — Alpha\ndate: "))
        self.assertIn("*[imported, claude-code] — import-claude-context | 2026-08-01 → 2026-08-05 | user*", note)
        self.assertEqual(len(json.loads((self.staged / "people.json").read_text())), 25)
        man = self._manifest()
        self.assertEqual(man["projects"], [SLUG_A, SLUG_B])
        self.assertEqual(man["run_kind"], "user")
        self.assertEqual(man["fingerprint"], self.m.inputs_fingerprint(self.data))
        # a second stage replaces the pending set (no leftovers, same result)
        rc, c2, _ = self._run("--stage", "--run-kind", "onboarding")
        self.assertEqual(c2, c)
        self.assertEqual(self._manifest()["run_kind"], "onboarding")
        self.assertSinksUntouched()

    def test_review_digest_lists_projects_people_and_footer(self):
        self._run()
        review = (self.staged / "review.md").read_text()
        self.assertTrue(review.startswith("# Claude Code import — review before it lands\n"))
        self.assertIn("3 sessions across 2 projects, 2026-07-20 → 2026-08-05", review)
        self.assertIn("Nothing below has been written to your memory, notes or People store yet.", review)
        self.assertIn(f"### Alpha (`{SLUG_A}`)\n", review)
        self.assertIn("dir `/Users/o/Projects/alpha` · 2 sessions (2026-08-01 → 2026-08-05) · status: active · note: new", review)
        self.assertIn("Alpha is where the owner built the widget builder across two sessions.", review)
        self.assertIn("Open threads:\n- ship the widget\n- write the launch post\n", review)
        self.assertIn("Decisions:\n- write the core in Rust — because startup time\n", review)
        self.assertIn(f"### Beta (`{SLUG_B}`)\n", review)
        self.assertIn("A launch plan", review)              # no summary: falls back to what_it_is
        self.assertIn("Open threads: none recorded.\nDecisions: none recorded.", review)
        people_section = review.split("## People it would add (25)\n", 1)[1]
        self.assertIn("- Ada Lovelace — investor; CTO · Analytical (3 citations)\n", people_section)
        self.assertIn("- Only Alpha — colleague (2 citations)\n", people_section)
        self.assertIn("- Person 00 — contact (2 citations)\n", people_section)
        self.assertNotIn("One Citation", review)
        self.assertIn("_Left out: 1 with a single mention only (2 citations needed)._", review)
        self.assertIn("## Memory\n\n", review)
        self.assertIn("B summary (cap 2,000 B) covering 2 projects for the agent's core memory, plus one MEMORY.md row if the index budget allows.", review)
        self.assertTrue(review.rstrip().endswith("---\n" + FOOTER))
        self.assertEqual(review.count(FOOTER), 1)

    def test_stage_needs_a_rollup(self):
        for p in (self.data / "projects").glob("*.json"):
            p.unlink()
        msg = self._refused()
        self.assertIn("nothing to stage", msg)
        self.assertFalse(self.staged.exists())
        self.assertSinksUntouched()


class TestCommit(Base):
    def test_commit_moves_everything_with_the_guards(self):
        self._run()
        staged_note = (self.staged / "notes" / "claude-import" / f"{SLUG_A}.md").read_text()
        rc, c, err = self._run("--commit")
        self.assertEqual(rc, 0)
        self.assertEqual((c["committed"], c["staged_remaining"], c["projects"]), (2, 0, 2))
        self.assertEqual((c["sessions"], c["summarized"], c["people"], c["people_merged"]), (3, 3, 25, 0))
        self.assertEqual((c["notes_created"], c["notes_updated"], c["notes_unchanged"]), (2, 0, 0))
        self.assertTrue(c["memory_row"])
        self.assertIn("row appended", err)
        mem = (self.mem / "claude_import.md").read_text()
        self.assertLessEqual(len(mem.encode()), 2000)
        self.assertEqual(c["memory_bytes"], len(mem.encode()))
        self.assertIn("Imported 3 sessions / 2 projects on ", mem)
        self.assertIn("- Beta — A launch plan; paused", mem)
        index = (self.mem / "MEMORY.md").read_text()
        self.assertIn("keep me", index)
        self.assertEqual(index.count("- [Claude Code history import](claude_import.md) — 2 projects, open threads, people"), 1)
        self.assertEqual((self.ndir / f"{SLUG_A}.md").read_text(), staged_note)   # reviewed text lands verbatim
        self.assertTrue((self.ndir / f"{SLUG_B}.md").is_file())
        self.assertIn(f"- [Beta]({SLUG_B}.md)", (self.ndir / "overview.md").read_text())
        self.assertFalse(self.staged.exists())
        self.assertEqual(self._status()["phase"], "done")
        state = json.loads((self.data / "state.json").read_text())
        for slug, u in ((SLUG_A, U1), (SLUG_A, U2), (SLUG_B, U3)):
            self.assertIn("summarized_at", state["sessions"][f"{slug}/{u}"])
        self.assertEqual(sorted(state["projects"]), [SLUG_A, SLUG_B])
        # a second commit has nothing to commit
        self.assertIn("nothing is staged", self._refused("--commit"))
        # re-run with a changed roll-up: staged as an update, committed as a dated section
        p = self.data / "projects" / f"{SLUG_A}.json"
        doc = json.loads(p.read_text())
        doc["note_markdown"] = "Alpha shipped the widget."
        p.write_text(json.dumps(doc))
        rc, c, _ = self._run()
        self.assertEqual((c["notes_created"], c["notes_updated"], c["notes_unchanged"]), (0, 1, 1))
        self.assertIn("note: update to the existing note", (self.staged / "review.md").read_text())
        self.assertIn("note: unchanged (nothing new)", (self.staged / "review.md").read_text())
        self.assertNotIn("Alpha shipped the widget.", (self.ndir / f"{SLUG_A}.md").read_text())
        rc, c, _ = self._run("--commit")
        self.assertEqual((c["notes_created"], c["notes_updated"], c["notes_unchanged"]), (0, 1, 1))
        note = (self.ndir / f"{SLUG_A}.md").read_text()
        self.assertEqual(note.count("## Update "), 1)
        self.assertIn(f"## Update {self.m.today()}\n\nAlpha shipped the widget.", note)
        self.assertIn("Alpha builds widgets.", note)
        self.assertEqual((self.mem / "MEMORY.md").read_text().count("](claude_import.md)"), 1)

    def test_commit_row_skipped_on_budget_refusal_file_kept(self):
        self._run()
        rc, c, err = self._run("--commit", runner=_refuse)
        self.assertEqual(rc, 0)
        self.assertFalse(c["memory_row"])
        self.assertIn("refused", err)
        self.assertTrue((self.mem / "claude_import.md").is_file())
        self.assertEqual((self.mem / "MEMORY.md").read_text(), "- [Existing lesson](existing.md) — keep me\n")
        self.assertEqual(self._status()["phase"], "done")
        self.assertFalse(self._status()["memory_row"])

    def test_commit_applies_the_memory_cap(self):
        index = json.loads((self.data / "index.json").read_text())
        for i in range(60):
            slug = f"-Users-o-Projects-long{i:02d}"
            index["projects"][slug] = {"slug": slug, "cwd": f"/Users/o/Projects/long{i:02d}", "session_count": 60 - i,
                                       "first_ts": "2026-06-01T00:00:00Z", "last_ts": "2026-06-02T00:00:00Z", "sessions": []}
            (self.data / "projects" / f"{slug}.json").write_text(json.dumps({
                "project": slug, "name": f"Project number {i:02d} with a long name", "status": "active",
                "what_it_is": "what it is " * 12, "top_open_thread": "open thread " * 12,
                "note_markdown": "long " * 50}))
        index["counts"] = {"sessions": 500, "projects": 62}
        (self.data / "index.json").write_text(json.dumps(index))
        rc, c, _ = self._run()
        self.assertEqual(c["projects"], 62)
        self.assertLessEqual(c["memory_bytes"], 2000)
        rc, c, _ = self._run("--commit")
        mem = (self.mem / "claude_import.md").read_text()
        self.assertLessEqual(len(mem.encode()), 2000)
        self.assertIn("Imported 500 sessions / 62 projects", mem)
        self.assertIn("more projects in notes/claude-import/overview.md", mem)
        self.assertEqual(len(list(self.ndir.glob("*.md"))), 63)

    def test_commit_projects_lands_one_and_keeps_the_other_staged(self):
        self._run()
        rc, c, _ = self._run("--commit", "--projects", "alpha")       # a unique part of the slug
        self.assertEqual(rc, 0)
        self.assertEqual((c["committed"], c["staged_remaining"], c["projects"]), (1, 1, 1))
        self.assertEqual((c["sessions"], c["summarized"]), (2, 2))
        self.assertEqual(c["people"], 2)      # Ada + Only Alpha: >= 2 citations inside Alpha; the "Person NN"s have one
        self.assertTrue((self.ndir / f"{SLUG_A}.md").is_file())
        self.assertFalse((self.ndir / f"{SLUG_B}.md").exists())
        overview = (self.ndir / "overview.md").read_text()
        self.assertIn("[Alpha]", overview)
        self.assertNotIn("[Beta]", overview)
        mem = (self.mem / "claude_import.md").read_text()
        self.assertIn("Imported 2 sessions / 1 projects", mem)
        self.assertIn("- Alpha —", mem)
        self.assertNotIn("- Beta", mem)
        self.assertIn("](claude_import.md) — 1 projects", (self.mem / "MEMORY.md").read_text())
        self.assertEqual(self._manifest()["projects"], [SLUG_B])
        self.assertTrue((self.staged / "notes" / "claude-import" / f"{SLUG_B}.md").is_file())
        self.assertFalse((self.staged / "notes" / "claude-import" / f"{SLUG_A}.md").exists())
        review = (self.staged / "review.md").read_text()
        self.assertIn(f"### Beta (`{SLUG_B}`)", review)
        self.assertNotIn("### Alpha", review)
        self.assertIn("covering 2 projects", review)         # the memory file spans landed + pending
        self.assertEqual(self._status()["phase"], "staged")
        self.assertEqual(self._status()["staged_remaining"], 1)
        state = json.loads((self.data / "state.json").read_text())
        self.assertIn("summarized_at", state["sessions"][f"{SLUG_A}/{U1}"])
        self.assertNotIn(f"{SLUG_B}/{U3}", state["sessions"])
        # people for the landed project only, quoting nothing from the pending one
        rc, payloads, _ = self._run("--people-json", "--projects", "alpha")
        self.assertEqual([p["name"] for p in payloads], ["Ada Lovelace", "Only Alpha"])
        self.assertNotIn("b3b3b3b3", payloads[0]["doc"])
        self.assertIn("a1a1a1a1", payloads[0]["doc"])
        # plain --people-json while a review is pending prints the staged copy
        rc, pending, _ = self._run("--people-json")
        self.assertEqual(pending, json.loads((self.staged / "people.json").read_text()))
        # ...then the rest
        rc, c, _ = self._run("--commit")
        self.assertEqual((c["committed"], c["staged_remaining"], c["projects"], c["sessions"]), (1, 0, 2, 3))
        self.assertIn("Imported 3 sessions / 2 projects", (self.mem / "claude_import.md").read_text())
        index = (self.mem / "MEMORY.md").read_text()
        self.assertIn("](claude_import.md) — 2 projects", index)
        self.assertEqual(index.count("](claude_import.md)"), 1)
        self.assertTrue((self.ndir / f"{SLUG_B}.md").is_file())
        self.assertFalse(self.staged.exists())
        self.assertEqual(self._status()["phase"], "done")
        rc, payloads, _ = self._run("--people-json")
        self.assertEqual(len(payloads), 25)

    def test_commit_selector_must_match_exactly_one(self):
        self._run()
        self.assertIn("matches 2 staged projects", self._refused("--commit", "--projects", "Projects"))
        self.assertIn("no staged project matches 'nope'", self._refused("--commit", "--projects", "nope"))
        self.assertSinksUntouched()
        self.assertEqual(self._manifest()["projects"], [SLUG_A, SLUG_B])
        # the exact slug, typed with its leading dash, is accepted
        rc, c, _ = self._run("--commit", "--projects", SLUG_B)
        self.assertEqual((c["committed"], c["staged_remaining"]), (1, 1))

    def test_stale_staged_set_is_refused(self):
        self._run()
        p = self.data / "projects" / f"{SLUG_A}.json"
        doc = json.loads(p.read_text())
        doc["note_markdown"] = "Alpha changed after the owner read the digest."
        p.write_text(json.dumps(doc))
        msg = self._refused("--commit")
        self.assertIn("stale", msg)
        self.assertSinksUntouched()
        self.assertTrue((self.staged / "review.md").is_file())
        self.assertFalse((self.data / "state.json").exists())
        # a summary change alone is enough
        self._run()
        (self.data / "summaries" / SLUG_B / f"{U3}.json").write_text(json.dumps({"session": U3, "summary": "new"}))
        self.assertIn("stale", self._refused("--commit"))
        # staging again clears it
        self._run()
        rc, c, _ = self._run("--commit")
        self.assertEqual(c["committed"], 2)
        self.assertIn("Alpha changed after the owner read the digest.", (self.ndir / f"{SLUG_A}.md").read_text())

    def test_commit_with_nothing_staged_is_refused(self):
        msg = self._refused("--commit")
        self.assertIn("nothing is staged", msg)
        self.assertSinksUntouched()
        self.assertFalse((self.data / "status.json").exists())
        # a discarded set is not a staged set either
        self._run()
        self._run("--discard")
        self.assertIn("nothing is staged", self._refused("--commit"))
        self.assertSinksUntouched()

    def test_finalize_helper_is_stage_plus_commit(self):
        with patch.object(self.m.subprocess, "run", side_effect=_ok), redirect_stderr(io.StringIO()):
            c = self.m.finalize(data_dir=self.data, ws=self.ws, memory_dir=self.mem, run_kind="onboarding")
        self.assertEqual((c["committed"], c["projects"], c["people_merged"]), (2, 2, 0))
        self.assertFalse(self.staged.exists())
        self.assertIn("| onboarding*", (self.ndir / f"{SLUG_A}.md").read_text())


class TestDiscardAndForget(Base):
    def test_discard_projects_drops_one(self):
        self._run()
        rc, r, _ = self._run("--discard", "--projects", "beta")
        self.assertEqual(rc, 0)
        self.assertEqual(r, {"discarded": 1, "staged_remaining": 1})
        self.assertEqual(self._manifest()["projects"], [SLUG_A])
        self.assertFalse((self.staged / "notes" / "claude-import" / f"{SLUG_B}.md").exists())
        self.assertTrue((self.staged / "notes" / "claude-import" / f"{SLUG_A}.md").is_file())
        review = (self.staged / "review.md").read_text()
        self.assertNotIn("### Beta", review)
        self.assertIn("### Alpha", review)
        self.assertEqual(self._status()["phase"], "staged")
        self.assertSinksUntouched()
        self.assertTrue((self.data / "projects" / f"{SLUG_B}.json").is_file())   # only the staged copy went
        rc, r, _ = self._run("--discard")
        self.assertEqual(r, {"discarded": 1, "staged_remaining": 0})
        self.assertFalse(self.staged.exists())
        self.assertEqual(self._status()["phase"], "discarded")
        self.assertSinksUntouched()
        self.assertIn("nothing is staged", self._refused("--discard"))

    def test_forget_rerenders_a_pending_review(self):
        self._run()
        rc, r, _ = self._run("--forget", SLUG_A)
        self.assertEqual(rc, 0)
        self.assertEqual((r["rollup"], r["staged"], r["note"]), (1, 1, 0))
        self.assertEqual(self._manifest()["projects"], [SLUG_B])
        review = (self.staged / "review.md").read_text()
        self.assertNotIn("### Alpha", review)
        self.assertNotIn("Only Alpha", review)
        self.assertNotIn("Ada Lovelace", review)          # one citation left after Alpha went
        self.assertSinksUntouched()
        rc, c, _ = self._run("--commit")                  # not stale: the review was re-rendered
        self.assertEqual((c["committed"], c["projects"]), (1, 1))
        self.assertTrue((self.ndir / f"{SLUG_B}.md").is_file())


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.m = _load()

    def test_shared_email_is_one_person(self):
        people = [
            {"name": "Michael", "email": "michael@actoneventures.com", "role": "Partner",
             "citations": [_cite(SLUG_A, U1, "intro call")]},
            {"name": "Michael Silton", "email": "michael@actoneventures.com", "role": "General Partner",
             "company": "Act One Ventures", "citations": [_cite(SLUG_A, U2, "term sheet")]},
        ]
        merged, n = self.m.merge_people(people)
        self.assertEqual(n, 1)
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["name"], "Michael Silton")
        self.assertEqual(m["email"], "michael@actoneventures.com")
        self.assertEqual(m["role"], "General Partner")          # the more specific one
        self.assertEqual(m["company"], "Act One Ventures")
        self.assertEqual([c["quote_or_context"] for c in m["citations"]], ["intro call", "term sheet"])
        self.assertNotIn("emails", m)                          # one address: no list
        # a second address on either side is unioned; identifiers too
        people[1]["emails"] = ["Michael@ActOne.vc"]
        people[1]["identifiers"] = {"x": "@silton"}
        m = self.m.merge_people(people)[0][0]
        self.assertEqual(m["emails"], ["michael@actoneventures.com", "Michael@ActOne.vc"])
        self.assertEqual(m["identifiers"], {"x": "@silton", "emails": ["michael@actoneventures.com", "Michael@ActOne.vc"]})

    def test_first_name_folds_into_its_unique_full_name(self):
        people = [
            {"name": "Qingyun", "citations": [_cite(SLUG_A, U1, "q1")]},
            {"name": "Qingyun Wu", "citations": [_cite(SLUG_A, U2, "q2")]},
            {"name": "Chi", "citations": [_cite(SLUG_B, U3, "c1")]},
            {"name": "Chi Wang", "role": "advisor", "citations": [_cite(SLUG_A, U1, "c2")]},
            {"name": "Alejandro", "citations": [_cite(SLUG_A, U1, "a1")]},
            {"name": "Alejandro Guerrero", "citations": [_cite(SLUG_A, U2, "a2"), _cite(SLUG_A, U1, "a1")]},
            {"name": "John", "citations": [_cite(SLUG_A, U1, "j0")]},
            {"name": "John Smith", "citations": [_cite(SLUG_A, U1, "j1")]},
            {"name": "John Doe", "citations": [_cite(SLUG_A, U2, "j2")]},
        ]
        before = copy.deepcopy(people)
        merged, n = self.m.merge_people(people)
        self.assertEqual(n, 3)
        self.assertEqual([p["name"] for p in merged],
                         ["Qingyun Wu", "Chi Wang", "Alejandro Guerrero", "John", "John Smith", "John Doe"])
        chi = merged[1]
        self.assertEqual(chi["role"], "advisor")
        self.assertEqual([c["quote_or_context"] for c in chi["citations"]], ["c2", "c1"])
        self.assertEqual([c["quote_or_context"] for c in merged[2]["citations"]], ["a2", "a1"])   # unioned, deduped
        self.assertEqual(people, before)                                                        # pure
        self.assertEqual(self.m.merge_people(people), (merged, n))                             # deterministic

    def test_case_insensitive_fold_keeps_the_fuller_spelling(self):
        merged, n = self.m.merge_people([{"name": "qingyun", "citations": []}, {"name": "Qingyun  Wu", "citations": []}])
        self.assertEqual((n, [p["name"] for p in merged]), (1, ["Qingyun Wu"]))

    def test_conflicting_emails_block_the_fold(self):
        people = [{"name": "Chi", "email": "chi@a.example", "citations": []},
                  {"name": "Chi Wang", "email": "chi.wang@b.example", "citations": []}]
        merged, n = self.m.merge_people(people)
        self.assertEqual((n, len(merged)), (0, 2))
        # one side without an email is not a conflict
        del people[0]["email"]
        merged, n = self.m.merge_people(people)
        self.assertEqual((n, merged[0]["name"], merged[0]["email"]), (1, "Chi Wang", "chi.wang@b.example"))

    def test_multi_token_names_never_merge_by_name(self):
        people = [{"name": "Chi Wang", "citations": [_cite(SLUG_A, U1)]},
                  {"name": "Chi Wang", "citations": [_cite(SLUG_A, U2)]}]
        self.assertEqual(self.m.merge_people(people)[1], 0)
        people[0]["email"] = people[1]["email"] = "chi@example.com"
        merged, n = self.m.merge_people(people)              # ...but a shared email still does
        self.assertEqual((n, len(merged), len(merged[0]["citations"])), (1, 1, 2))
        # a bare token with two fuller matches stays, and so do both matches
        people = [{"name": "Ada"}, {"name": "Ada Lovelace"}, {"name": "Ada Byron"}]
        self.assertEqual(self.m.merge_people(people)[1], 0)
        # junk entries are dropped, not merged
        self.assertEqual(self.m.merge_people([None, "x", {"name": "Solo"}])[0], [{"name": "Solo"}])

    def test_email_inside_a_name_is_an_identifier_not_a_name(self):
        # Seen on the owner's real history: the summariser wrote "Cyrus (cyrus@x.com)".
        people = [{"name": "Cyrus (cyrus@onspark.com)", "citations": [_cite(SLUG_A, U1)]},
                  {"name": "Cyrus", "email": "cyrus@onspark.com", "citations": [_cite(SLUG_A, U2)]},
                  {"name": "Rui <rui@ag2.ai>", "citations": [_cite(SLUG_B, U3)]}]
        merged, n = self.m.merge_people(people)
        names = sorted(p["name"] for p in merged)
        self.assertEqual((n, names), (1, ["Cyrus", "Rui"]))
        cyrus = next(p for p in merged if p["name"] == "Cyrus")
        self.assertEqual(len(cyrus["citations"]), 2)
        self.assertIn("cyrus@onspark.com", [e.lower() for e in self.m._emails_of(cyrus)])
        rui = next(p for p in merged if p["name"] == "Rui")
        self.assertIn("rui@ag2.ai", [e.lower() for e in self.m._emails_of(rui)])

    def test_companies_merge_by_name(self):
        companies = [{"name": "Analytical", "citations": [_cite(SLUG_A, U1, "x")]},
                     {"name": "analytical ", "what": "engines", "relationship": "customer",
                      "citations": [_cite(SLUG_B, U3, "y"), _cite(SLUG_A, U1, "x")]},
                     {"name": "Other Co", "citations": []}]
        merged, n = self.m.merge_companies(companies)
        self.assertEqual(n, 1)
        self.assertEqual([c["name"] for c in merged], ["Analytical", "Other Co"])
        self.assertEqual(merged[0]["what"], "engines")
        self.assertEqual([c["quote_or_context"] for c in merged[0]["citations"]], ["x", "y"])
        self.assertEqual(self.m.merge_companies(companies), (merged, n))
        ents, counts = self.m.merge_entities({"people": [], "companies": companies, "deals": []})
        self.assertEqual(counts, {"people_merged": 0, "companies_merged": 1})
        self.assertEqual(ents["deals"], [])


class TestMergeInTheFlow(Base):
    def test_duplicates_are_folded_before_selection_and_reported(self):
        ents_path = self.data / "entities.json"
        ents = json.loads(ents_path.read_text())
        ents["people"].append({"name": "Ada", "email": "ADA@example.com",
                               "citations": [_cite(SLUG_B, U3, "asked about the bridge round")]})
        ents["people"].append({"name": "Only", "citations": [_cite(SLUG_A, U1, "standup")]})   # -> "Only Alpha"
        ents["companies"].append({"name": "ANALYTICAL", "citations": [_cite(SLUG_B, U3)]})
        ents_path.write_text(json.dumps(ents))
        raw = ents_path.read_bytes()
        rc, c, _ = self._run()
        self.assertEqual((c["people_merged"], c["companies_merged"], c["people"]), (2, 1, 25))
        self.assertEqual(ents_path.read_bytes(), raw)         # never written back
        review = (self.staged / "review.md").read_text()
        self.assertIn("- Ada Lovelace — investor; CTO · Analytical (4 citations)\n", review)
        self.assertIn("- Only Alpha — colleague (3 citations)\n", review)
        self.assertNotIn("- Ada —", review)
        self.assertIn("_2 duplicate people entries were merged first", review)
        ada = json.loads((self.staged / "people.json").read_text())[0]
        self.assertEqual(ada["name"], "Ada Lovelace")
        self.assertIn("asked about the bridge round", ada["doc"])
        self.assertEqual(ada["identifiers"], {"emails": ["ada@example.com"]})
        rc, c, _ = self._run("--commit")
        self.assertEqual((c["people_merged"], c["companies_merged"]), (2, 1))
        self.assertEqual(self._status()["people_merged"], 2)
        rc, payloads, _ = self._run("--people-json")
        self.assertEqual(payloads[0]["name"], "Ada Lovelace")
        self.assertEqual(sum(1 for p in payloads if p["name"].startswith("Ada")), 1)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

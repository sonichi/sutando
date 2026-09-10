#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/summarize.py — the direct-Gemini
summariser that replaces the per-session haiku subagents (no network: the HTTP
call is monkeypatched).

Pins: no credential -> exit 3 and `{"gemini": "unavailable"}` (SKILL.md then
takes the haiku path); the request body carries `responseSchema`, the JSON mime
type and the prompt's slug/uuid/title/cwd fields plus the transcript; summaries
land at summaries/<slug>/<uuid>.json with mode 0600, `session`/`project` set
from the index, and are skipped on a re-run (redone with --force); skipped_empty
and dump-less sessions get no request; a 429 is retried and then succeeds; a 400
counts as an error while the other sessions still complete (exit 1); a session
over --max-chars goes as ordered parts + one merge request with partials on
disk; projects/<slug>.json (with note_markdown, cwd and sessions from disk) and
entities.json land where finalize.py reads them and finalize --stage renders a
review from them; status.json is recounted by progress.py after the stages; the
key never appears in the output.

Run: python3 tests/import-claude-context-summarize.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
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
U1 = "a1a1a1a1-0000-4000-8000-000000000001"
U2 = "a2a2a2a2-0000-4000-8000-000000000002"
U3 = "b3b3b3b3-0000-4000-8000-000000000003"
U4 = "c4c4c4c4-0000-4000-8000-000000000004"  # skipped_empty: no dump, no request
U5 = "d5d5d5d5-0000-4000-8000-000000000005"  # indexed, never extracted: no dump, no request
FAKE_KEY = "AIzaFAKEKEYFORTESTS0000000000000000000"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"ici_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _j(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def _session(uuid, title, first, last, cwd="/Users/o/Projects/alpha"):
    return {"uuid": uuid, "file": f"{uuid}.jsonl", "title": title, "cwd": cwd, "first_ts": first,
            "last_ts": last, "user_msgs": 3, "assistant_msgs": 3}


def make_data(data: Path) -> None:
    """Two projects, three dumped sessions (U2 with two chunks), one skipped_empty, one never extracted."""
    _j(data / "index.json", {
        "generated_at": "2026-09-10T00:00:00Z", "root": "/Users/o/.claude/projects",
        "counts": {"sessions": 5, "projects": 2},
        "projects": {
            SLUG_A: {"slug": SLUG_A, "cwd": "/Users/o/Projects/alpha", "session_count": 4,
                     "first_ts": "2026-08-01T10:00:00Z", "last_ts": "2026-08-06T10:00:00Z",
                     "sessions": [_session(U1, "alpha widget builder", "2026-08-01T10:00:00Z", "2026-08-01T12:00:00Z"),
                                  _session(U2, "alpha launch post", "2026-08-05T10:00:00Z", "2026-08-05T11:00:00Z"),
                                  _session(U4, "aborted", "2026-08-06T09:00:00Z", "2026-08-06T09:00:00Z"),
                                  _session(U5, "never extracted", "2026-08-06T10:00:00Z", "2026-08-06T10:00:00Z")]},
            SLUG_B: {"slug": SLUG_B, "cwd": "/Users/o/Projects/beta", "session_count": 1,
                     "first_ts": "2026-08-03T10:00:00Z", "last_ts": "2026-08-03T10:00:00Z",
                     "sessions": [_session(U3, "beta pricing call", "2026-08-03T10:00:00Z",
                                           "2026-08-03T11:00:00Z", cwd="/Users/o/Projects/beta")]},
        },
    })
    _j(data / "state.json", {"sessions": {
        f"{SLUG_A}/{U1}": {"extracted_at": "2026-09-10T00:00:00Z", "chunks": 1, "chars": 900},
        f"{SLUG_A}/{U2}": {"extracted_at": "2026-09-10T00:00:00Z", "chunks": 2, "chars": 1800},
        f"{SLUG_A}/{U4}": {"extracted_at": "2026-09-10T00:00:00Z", "chunks": 0, "chars": 0, "skipped_empty": True},
        f"{SLUG_B}/{U3}": {"extracted_at": "2026-09-10T00:00:00Z", "chunks": 1, "chars": 900},
    }, "projects": {}})
    dumps = data / "dumps"
    for slug, uuid, n, body in ((SLUG_A, U1, 1, "[2026-08-01T10:00:00Z] USER: build the widget\n[2026-08-01T10:01:00Z] ASSISTANT: ok, done in Rust\n"),
                                (SLUG_A, U2, 1, "[2026-08-05T10:00:00Z] USER: write the launch post\n"),
                                (SLUG_A, U2, 2, "[2026-08-05T10:30:00Z] ASSISTANT: drafted with Alice Chen (alice@example.com)\n"),
                                (SLUG_B, U3, 1, "[2026-08-03T10:00:00Z] USER: pricing call with Bob Ray of Acme\n")):
        p = dumps / slug / f"{uuid}.{n}.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# claude-import · project {slug} · session {uuid} · chunk {n}\n\n" + body)


def _session_doc(uuid, slug):
    return {"session": "model-guess", "project": "model-guess", "title": f"session {uuid[:2]}",
            "date_range": ["2026-08-01T10:00:00Z", "2026-08-01T12:00:00Z"],
            "summary": "The owner built a thing.", "timeline": [{"when": "", "what": "built"}],
            "tasks": [{"task": "build", "outcome": "done", "detail": ""}], "prs_commits": [],
            "decisions": [{"decision": "Rust", "why": "startup time"}],
            "errors_fixes": [], "artifacts": [], "loose_ends": ["ship it"],
            "people": [{"name": "Alice Chen", "role_or_relationship": "colleague", "context": "", "email": "alice@example.com"}],
            "companies": []}


def _project_doc(slug):
    return {"project": "model-guess", "name": "Alpha", "cwd": "model-guess", "what_it_is": "a widget builder",
            "status": "active", "summary": "Alpha is the widget builder.", "top_open_thread": "ship the widget",
            "open_threads": ["ship the widget"], "key_decisions": [{"decision": "Rust", "why": "startup time"}],
            "accomplished": ["built the widget"], "prs_commits": [], "people": [{"name": "Alice Chen", "role_or_relationship": "colleague"}],
            "companies": ["Acme"], "sessions": ["model-guess"],
            "note_markdown": "Alpha is a widget builder.\n\nBuilt the widget. Decided on Rust for startup time."}


def _entities_doc():
    cite = lambda s, p: {"project": p, "session": s, "quote_or_context": "worked together"}  # noqa: E731
    return {"generated_at": "model-guess",
            "people": [{"name": "Alice Chen", "email": "alice@example.com", "company": "", "role": "designer",
                        "relationship": "colleague", "citations": [cite(U1, SLUG_A), cite(U2, SLUG_A)]},
                       {"name": "Bob Ray", "email": "", "company": "Acme", "role": "buyer",
                        "relationship": "customer", "citations": [cite(U3, SLUG_B)]}],
            "companies": [{"name": "Acme", "what": "a customer", "relationship": "customer", "citations": [cite(U3, SLUG_B)]}],
            "deals": [], "decisions": [{"decision": "Rust", "why": "startup time", "project": SLUG_A, "citations": [cite(U1, SLUG_A)]}],
            "open_threads": [{"thread": "ship the widget", "project": SLUG_A, "owner_action": "ship", "citations": [cite(U2, SLUG_A)]}]}


def _reply(doc, prompt_tokens=100, cand_tokens=50):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(doc)}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": cand_tokens,
                              "totalTokenCount": prompt_tokens + cand_tokens}}


class FakeGemini:
    """Stands in for summarize._http_post: records every request, answers by schema."""

    def __init__(self, m, script=None):
        self.m = m
        self.calls = []  # (url, key, payload)
        self.script = list(script or [])  # optional per-call exceptions / replies, consumed in order

    def __call__(self, url, key, payload, timeout):
        self.calls.append((url, key, payload, timeout))
        if self.script:
            nxt = self.script.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            if nxt is not None:
                return nxt
        schema = payload["generationConfig"]["responseSchema"]
        if schema is self.m.SESSION_SCHEMA:
            return _reply(_session_doc("x", "y"))
        if schema is self.m.PROJECT_SCHEMA:
            return _reply(_project_doc("y"))
        if schema is self.m.ENTITIES_SCHEMA:
            return _reply(_entities_doc())
        raise AssertionError("unknown schema")


class Base(unittest.TestCase):
    def setUp(self):
        self.m = _load("summarize")
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "ws"
        self.data = self.ws / "data" / "claude-import"
        make_data(self.data)
        self.fake = FakeGemini(self.m)
        self.env = patch.dict(os.environ, {"GEMINI_API_KEY": FAKE_KEY}, clear=False)
        self.env.start()
        os.environ.pop("GEMINI_VOICE_API_KEY", None)

    def tearDown(self):
        self.env.stop()

    def _run(self, *args, fake=None):
        out, err = io.StringIO(), io.StringIO()
        fake = fake or self.fake
        with patch.object(self.m, "_http_post", fake), patch.object(self.m, "_sleep", lambda s: None), \
                redirect_stdout(out), redirect_stderr(err):
            rc = self.m.main(["--workspace", str(self.ws), "--json", *args])
        return rc, json.loads(out.getvalue()), err.getvalue()

    def _summary(self, slug, uuid):
        return json.loads((self.data / "summaries" / slug / f"{uuid}.json").read_text())


class TestCredential(Base):
    def test_no_credential_exits_3_and_writes_nothing(self):
        os.environ.pop("GEMINI_API_KEY", None)
        rc, r, _ = self._run()
        self.assertEqual(rc, 3)
        self.assertEqual(r["gemini"], "unavailable")
        self.assertEqual(self.fake.calls, [])
        self.assertFalse((self.data / "summaries").exists())

    def test_managed_key_wins_over_env(self):
        _j(self.ws / "state" / "auth" / "managed-credentials.json",
           {"version": 1, "capabilities": {"gemini-text": {"key": "managed-key"}}})
        rc, r, _ = self._run("--stage", "sessions")
        self.assertEqual(rc, 0)
        self.assertEqual(r["source"], "managed")
        self.assertTrue(all(key == "managed-key" for _, key, _, _ in self.fake.calls))

    def test_key_never_in_output(self):
        rc, r, err = self._run()
        self.assertNotIn(FAKE_KEY, json.dumps(r) + err)
        self.assertNotIn(FAKE_KEY, (self.data / "status.json").read_text())


class TestSessions(Base):
    def test_requests_carry_schema_prompt_fields_and_transcript(self):
        rc, r, _ = self._run("--stage", "sessions")
        self.assertEqual(rc, 0)
        self.assertEqual(r["stages"]["sessions"]["done"], 3)
        self.assertEqual(len(self.fake.calls), 3)
        url, key, payload, timeout = self.fake.calls[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(key, FAKE_KEY)
        self.assertEqual(timeout, 240)
        gen = payload["generationConfig"]
        self.assertEqual(gen["responseMimeType"], "application/json")
        self.assertIs(gen["responseSchema"], self.m.SESSION_SCHEMA)
        self.assertEqual(gen["temperature"], 0.2)
        self.assertEqual(gen["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(gen["maxOutputTokens"], 32768)
        self.assertEqual(set(gen["responseSchema"]["required"]),
                         {"session", "project", "title", "date_range", "summary", "timeline", "tasks", "prs_commits",
                          "decisions", "errors_fixes", "artifacts", "loose_ends", "people", "companies"})
        texts = {c[2]["contents"][0]["parts"][0]["text"] for c in self.fake.calls}
        u2 = next(t for t in texts if f"Session: `{U2}`" in t)
        self.assertIn(f"Project slug: `{SLUG_A}` (working dir `/Users/o/Projects/alpha`)", u2)
        self.assertIn('title from the index: "alpha launch post"', u2)
        self.assertIn("=== TRANSCRIPT ===", u2)
        self.assertIn("write the launch post", u2)
        self.assertIn("drafted with Alice Chen", u2)      # both chunks, in order
        self.assertLess(u2.index("write the launch post"), u2.index("drafted with Alice Chen"))
        self.assertNotIn("Read tool", u2)
        self.assertNotIn("map-reduce", u2)
        self.assertNotIn("Reply to the caller", u2)

    def test_summaries_written_0600_with_index_identity_and_skipped_on_rerun(self):
        rc, r, _ = self._run("--stage", "sessions")
        self.assertEqual(rc, 0)
        s = r["stages"]["sessions"]
        self.assertEqual((s["total"], s["done"], s["skipped"], s["errors"], s["skipped_empty"], s["no_dumps"]),
                         (3, 3, 0, 0, 1, 1))
        p = self.data / "summaries" / SLUG_A / f"{U2}.json"
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        doc = self._summary(SLUG_A, U2)
        self.assertEqual((doc["session"], doc["project"]), (U2, SLUG_A))   # from the index, not the model
        self.assertEqual(doc["people"][0]["email"], "alice@example.com")
        self.assertFalse((self.data / "summaries" / SLUG_A / f"{U4}.json").exists())
        self.assertFalse((self.data / "summaries" / SLUG_A / f"{U5}.json").exists())
        self.assertEqual(r["usage"]["prompt_tokens"], 300)
        self.assertEqual(r["usage"]["candidate_tokens"], 150)
        # re-run: nothing newer than the summaries -> no request
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "sessions", fake=fake2)
        self.assertEqual(rc, 0)
        self.assertEqual((r["stages"]["sessions"]["done"], r["stages"]["sessions"]["skipped"]), (0, 3))
        self.assertEqual(fake2.calls, [])
        # --force redoes them
        fake3 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "sessions", "--force", fake=fake3)
        self.assertEqual(r["stages"]["sessions"]["done"], 3)
        self.assertEqual(len(fake3.calls), 3)

    def test_re_extracted_session_is_redone(self):
        self._run("--stage", "sessions")
        st = json.loads((self.data / "state.json").read_text())
        st["sessions"][f"{SLUG_B}/{U3}"]["extracted_at"] = "2999-01-01T00:00:00Z"
        _j(self.data / "state.json", st)
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "sessions", fake=fake2)
        self.assertEqual((r["stages"]["sessions"]["done"], r["stages"]["sessions"]["skipped"]), (1, 2))
        self.assertIn(f"Session: `{U3}`", fake2.calls[0][2]["contents"][0]["parts"][0]["text"])

    def test_selectors(self):
        rc, r, _ = self._run("--stage", "sessions", "--projects", "beta")
        self.assertEqual(r["stages"]["sessions"]["done"], 1)
        self.assertTrue((self.data / "summaries" / SLUG_B / f"{U3}.json").exists())
        self.assertFalse((self.data / "summaries" / SLUG_A).exists())
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "sessions", "--session", U1[:8], fake=fake2)
        self.assertEqual(r["stages"]["sessions"]["done"], 1)
        self.assertTrue((self.data / "summaries" / SLUG_A / f"{U1}.json").exists())

    def test_429_then_success_is_retried(self):
        fake = FakeGemini(self.m, script=[self.m._Retryable(429, "http 429", retry_after=0.0)])
        slept = []
        with patch.object(self.m, "_sleep", slept.append):
            out = io.StringIO()
            with patch.object(self.m, "_http_post", fake), redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = self.m.main(["--workspace", str(self.ws), "--json", "--stage", "sessions", "--concurrency", "1"])
        r = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(r["stages"]["sessions"]["done"], 3)
        self.assertEqual(r["stages"]["sessions"]["errors"], 0)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(r["retries"]["429"], 1)
        self.assertEqual(r["requests"], 4)
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 1.0)

    def test_429_exhausted_after_five_attempts(self):
        fake = FakeGemini(self.m, script=[self.m._Retryable(429, "http 429")] * 5)
        rc, r, _ = self._run("--stage", "sessions", "--concurrency", "1", fake=fake)
        self.assertEqual(rc, 1)
        self.assertEqual((r["stages"]["sessions"]["done"], r["stages"]["sessions"]["errors"]), (2, 1))
        self.assertEqual(r["retries"]["429"], 5)
        self.assertEqual(len(fake.calls), 7)   # 5 attempts for the first session + 2 good ones

    def test_400_counts_as_error_and_run_continues(self):
        fake = FakeGemini(self.m, script=[self.m.GeminiError(400, "http 400")])
        rc, r, _ = self._run("--stage", "sessions", "--concurrency", "1", fake=fake)
        self.assertEqual(rc, 1)
        s = r["stages"]["sessions"]
        self.assertEqual((s["done"], s["errors"]), (2, 1))
        self.assertEqual(r["errors"], 1)
        self.assertEqual(r["error_kinds"], [{"stage": "sessions", "status": 400, "kind": "http 400"}])
        self.assertEqual(len(fake.calls), 3)     # no retry on a 400
        self.assertEqual(r["retries"], {"429": 0, "5xx": 0, "transport": 0, "other": 0})
        self.assertEqual(len(list((self.data / "summaries").glob("*/*.json"))), 2)
        # the failed one is picked up by the next run
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "sessions", fake=fake2)
        self.assertEqual(rc, 0)
        self.assertEqual((r["stages"]["sessions"]["done"], r["stages"]["sessions"]["skipped"]), (1, 2))

    def test_blocked_or_truncated_reply_is_an_error_not_a_retry(self):
        fake = FakeGemini(self.m, script=[{"promptFeedback": {"blockReason": "OTHER"}, "candidates": []}])
        rc, r, _ = self._run("--stage", "sessions", "--concurrency", "1", fake=fake)
        self.assertEqual(r["stages"]["sessions"]["errors"], 1)
        self.assertEqual(r["error_kinds"][0]["kind"], "prompt blocked")
        self.assertEqual(len(fake.calls), 3)

    def test_long_session_goes_as_parts_plus_merge(self):
        # U2's two chunks (~150 B each) do not fit one 200-char request: two part
        # requests, partials on disk, then one merge request writes the merged file
        rc, r, _ = self._run("--stage", "sessions", "--max-chars", "200", "--concurrency", "1")
        self.assertEqual(rc, 0)
        s = r["stages"]["sessions"]
        self.assertEqual((s["done"], s["requests"], s["split_parts"]), (3, 5, 2))
        texts = [c[2]["contents"][0]["parts"][0]["text"] for c in self.fake.calls]
        parts = [t for t in texts if "PART 1 of 2" in t or "PART 2 of 2" in t]
        self.assertEqual(len(parts), 2)
        merge = [t for t in texts if "=== PARTIAL SUMMARIES" in t]
        self.assertEqual(len(merge), 1)
        self.assertIn(f"Session: `{U2}`", merge[0])
        self.assertIs(self.fake.calls[-1][2]["generationConfig"]["responseSchema"], self.m.SESSION_SCHEMA)
        d = self.data / "summaries" / SLUG_A
        self.assertTrue((d / f"{U2}.1.json").exists() and (d / f"{U2}.2.json").exists())
        self.assertEqual(stat.S_IMODE((d / f"{U2}.1.json").stat().st_mode), 0o600)
        self.assertEqual(self._summary(SLUG_A, U2)["session"], U2)


class TestRollupsAndEntities(Base):
    def test_projects_and_entities_land_where_finalize_reads_them(self):
        rc, r, _ = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(r["stages"]["projects"]["done"], 2)
        self.assertEqual(r["stages"]["entities"]["done"], 1)
        self.assertEqual(r["requests"], 3 + 2 + 1)
        # the project request carries the prompt fields + that project's summaries only
        pcalls = [c[2] for c in self.fake.calls if c[2]["generationConfig"]["responseSchema"] is self.m.PROJECT_SCHEMA]
        t_alpha = next(p["contents"][0]["parts"][0]["text"] for p in pcalls if f"Project slug: `{SLUG_A}`" in p["contents"][0]["parts"][0]["text"])
        self.assertIn("(working dir `/Users/o/Projects/alpha`, 2 sessions, 2026-08-01 → 2026-08-05)", t_alpha)
        self.assertIn("Run kind: `full`", t_alpha)
        self.assertIn("=== SESSION SUMMARIES", t_alpha)
        self.assertIn(U1, t_alpha)
        self.assertNotIn(U3, t_alpha)
        self.assertNotIn("Read tool", t_alpha)
        ecall = next(c[2] for c in self.fake.calls if c[2]["generationConfig"]["responseSchema"] is self.m.ENTITIES_SCHEMA)
        t_ent = ecall["contents"][0]["parts"][0]["text"]
        self.assertIn(U1, t_ent)
        self.assertIn(U3, t_ent)
        self.assertNotIn("PREVIOUS ENTITIES", t_ent)
        # on disk, in finalize's shape
        rollup = json.loads((self.data / "projects" / f"{SLUG_A}.json").read_text())
        self.assertEqual((rollup["project"], rollup["cwd"]), (SLUG_A, "/Users/o/Projects/alpha"))
        self.assertEqual(rollup["sessions"], [U1, U2])
        self.assertIn("note_markdown", rollup)
        ent = json.loads((self.data / "entities.json").read_text())
        self.assertNotEqual(ent["generated_at"], "model-guess")
        self.assertEqual(len(ent["people"]), 2)
        fin = _load("finalize")
        index_doc, summaries, rollups, entities, _state = fin.load_inputs(self.data)
        self.assertEqual(sorted(rollups), sorted([SLUG_A, SLUG_B]))
        self.assertEqual(len(summaries), 3)
        self.assertEqual(len(entities["people"]), 2)
        # finalize --stage renders the review from them
        mem = self.tmp / "memory"
        mem.mkdir()
        ok = lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=0, stdout="✓ safe\n")  # noqa: E731
        out = io.StringIO()
        with patch.object(fin.subprocess, "run", side_effect=ok), redirect_stdout(out), redirect_stderr(io.StringIO()):
            frc = fin.main(["--workspace", str(self.ws), "--memory-dir", str(mem), "--json", "--stage", "--run-kind", "user"])
        self.assertEqual(frc, 0)
        c = json.loads(out.getvalue())
        # U5 is indexed but never extracted: progress/finalize count it as pending (4), not skipped
        self.assertEqual((c["sessions"], c["summarized"], c["projects"]), (4, 3, 2))
        review = (self.data / "staged" / "review.md").read_text()
        self.assertIn("Alice Chen", review)
        self.assertTrue((self.data / "staged" / "notes" / "claude-import" / f"{SLUG_A}.md").exists())
        self.assertEqual(json.loads((self.data / "status.json").read_text())["phase"], "staged")

    def test_status_recounted_after_each_stage(self):
        rc, r, _ = self._run("--stage", "sessions")
        st = json.loads((self.data / "status.json").read_text())
        # 3 of 4 (U5 was indexed but never extracted): still `summarizing`, counted from disk
        self.assertEqual((st["phase"], st["summarized"], st["sessions"], st["rolled_up"]), ("summarizing", 3, 4, 0))
        rc, r, _ = self._run("--stage", "projects")
        st = json.loads((self.data / "status.json").read_text())
        self.assertEqual((st["rolled_up"], st["entities"]), (2, False))
        rc, r, _ = self._run("--stage", "entities")
        st = json.loads((self.data / "status.json").read_text())
        self.assertEqual((st["rolled_up"], st["entities"]), (2, True))
        for k in st:
            self.assertNotIsInstance(st[k], (list, dict))

    def test_rollups_and_entities_resumable_and_incremental(self):
        self._run()
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run(fake=fake2)
        self.assertEqual(fake2.calls, [])
        self.assertEqual((r["stages"]["projects"]["skipped"], r["stages"]["entities"]["skipped"]), (2, 1))
        # a newer summary in alpha: alpha is rolled up again (incremental, with the
        # previous roll-up), beta is not; entities re-run with the previous pass
        p = self.data / "summaries" / SLUG_A / f"{U1}.json"
        os.utime(p, None)
        fake3 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "projects", fake=fake3)
        self.assertEqual((r["stages"]["projects"]["done"], r["stages"]["projects"]["skipped"]), (1, 1))
        text = fake3.calls[0][2]["contents"][0]["parts"][0]["text"]
        self.assertIn("Run kind: `incremental`", text)
        self.assertIn("=== PREVIOUS ROLL-UP", text)
        fake4 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "entities", fake=fake4)
        self.assertEqual(r["stages"]["entities"]["done"], 1)
        self.assertIn("=== PREVIOUS ENTITIES", fake4.calls[0][2]["contents"][0]["parts"][0]["text"])

    def test_entities_split_into_groups_then_merged_in_code(self):
        self._run("--stage", "sessions")
        # each group answers with the same two people; Bob gets a different citation per group
        cites = iter([U1, U2, U3])
        def per_group(url, key, payload, timeout):
            doc = _entities_doc()
            u = next(cites)
            doc["people"][1]["citations"] = [{"project": SLUG_B, "session": u, "quote_or_context": f"seen in {u[:4]}"}]
            doc["people"].append({"name": "Bob", "email": "", "company": "", "role": "", "relationship": "same person, bare first name",
                                  "citations": [{"project": SLUG_B, "session": u, "quote_or_context": "bob again"}]})
            return _reply(doc)
        fake2 = FakeGemini(self.m, script=[per_group(None, None, None, None) for _ in range(3)])
        rc, r, _ = self._run("--stage", "entities", "--entities-group-chars", "900", fake=fake2)
        self.assertEqual(rc, 0)
        s = r["stages"]["entities"]
        self.assertEqual((s["done"], s["requests"], s["split_parts"]), (1, 3, 3))   # one request per group, no merge request
        self.assertTrue(all(c[2]["generationConfig"]["responseSchema"] is self.m.ENTITIES_SCHEMA for c in fake2.calls))
        self.assertTrue(all("PARTIAL" not in c[2]["contents"][0]["parts"][0]["text"] for c in fake2.calls))
        ent = json.loads((self.data / "entities.json").read_text())
        names = sorted(p["name"] for p in ent["people"])
        self.assertEqual(names, ["Alice Chen", "Bob Ray"])                 # duplicates + "Bob" folded in code
        bob = next(p for p in ent["people"] if p["name"] == "Bob Ray")
        self.assertEqual(len(bob["citations"]), 6)                          # 3 groups x (Bob Ray + Bob), all kept
        self.assertEqual(len(ent["companies"]), 1)
        self.assertEqual(len(ent["decisions"]), 1)
        self.assertEqual(len(ent["open_threads"]), 1)
        self.assertNotEqual(ent["generated_at"], "model-guess")

    def test_big_project_rolled_up_in_parts_plus_merge(self):
        self._run("--stage", "sessions")
        fake2 = FakeGemini(self.m)
        rc, r, _ = self._run("--stage", "projects", "--group-max-chars", "700", "--concurrency", "1", fake=fake2)
        self.assertEqual(rc, 0)
        s = r["stages"]["projects"]
        self.assertEqual((s["done"], s["requests"], s["split_parts"]), (2, 4, 2))   # alpha: 2 parts + merge; beta: 1
        texts = [c[2]["contents"][0]["parts"][0]["text"] for c in fake2.calls]
        parts = [t for t in texts if "PART 1 of 2" in t or "PART 2 of 2" in t]
        self.assertEqual(len(parts), 2)
        self.assertIn(U1, parts[0])
        self.assertNotIn(U2, parts[0])
        merge = [t for t in texts if "=== PARTIAL ROLL-UPS" in t]
        self.assertEqual(len(merge), 1)
        self.assertIn(f"Project slug: `{SLUG_A}` (working dir `/Users/o/Projects/alpha`, 2 sessions, 2026-08-01 → 2026-08-05)", merge[0])
        self.assertTrue(all(c[2]["generationConfig"]["responseSchema"] is self.m.PROJECT_SCHEMA for c in fake2.calls))
        self.assertTrue(all(c[2]["generationConfig"]["maxOutputTokens"] == 16384 + 2048 for c in fake2.calls))
        self.assertTrue(all(c[2]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 2048} for c in fake2.calls))
        rollup = json.loads((self.data / "projects" / f"{SLUG_A}.json").read_text())
        self.assertEqual(rollup["sessions"], [U1, U2])
        self.assertEqual(sorted(f.name for f in (self.data / "projects").iterdir()), sorted([f"{SLUG_A}.json", f"{SLUG_B}.json"]))

    def test_truncated_reply_is_an_error(self):
        trunc = {"candidates": [{"content": {"parts": [{"text": '{"session": "x", "project": "y", "title": "cut'}]},
                                 "finishReason": "MAX_TOKENS"}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5}}
        fake = FakeGemini(self.m, script=[trunc])
        rc, r, _ = self._run("--stage", "sessions", "--concurrency", "1", fake=fake)
        self.assertEqual(rc, 1)
        self.assertEqual(r["stages"]["sessions"]["errors"], 1)
        self.assertEqual(r["error_kinds"][0]["kind"], "reply truncated (max tokens)")
        self.assertEqual(len(fake.calls), 3)

    def test_human_line_is_counts_only(self):
        out = io.StringIO()
        with patch.object(self.m, "_http_post", self.fake), patch.object(self.m, "_sleep", lambda s: None), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = self.m.main(["--workspace", str(self.ws)])
        line = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("sessions 3/3 done", line)
        self.assertIn("entities 1/1 done", line)
        self.assertNotIn("alpha launch post", line)
        self.assertNotIn(SLUG_A, line)
        self.assertNotIn(FAKE_KEY, line)


if __name__ == "__main__":
    unittest.main()

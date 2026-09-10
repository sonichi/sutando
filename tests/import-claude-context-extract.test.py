#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/extract.py — cleaned, redacted,
chunked dialog dumps built on session-recap's extract.py --root.

Pins: system-reminder blocks and watcher-ping lines stripped, `ghp_…` and
`AIza…` redacted before disk, chunks split only at `[ts] USER:/ASSISTANT:`
turn lines, 0600 files in a 0700 dir, --new bookkeeping on (mtime,size),
--session prefix, the --max-chars-total soft cap, and the counting rule from
the 2026-09-10 fresh-install run: a session with no cleaned dialog or under
--min-chars of it writes no dump, is `skipped_empty` (remembered in
state.json so --new leaves it alone) and is never counted as extracted.

Run: python3 tests/import-claude-context-extract.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")

SLUG = "-Users-o-Projects-alpha"
S1 = "aaaaaaaa-1111-4111-8111-111111111111"
S2 = "bbbbbbbb-2222-4222-8222-222222222222"
S3 = "cccccccc-3333-4333-8333-333333333333"   # aborted: only harness noise, no assistant turn
S4 = "dddddddd-4444-4444-8444-444444444444"   # answered, but ~60 chars of dialog
GHP = "ghp_" + "A1b2C3d4" * 5          # 40 chars after the prefix
AIZA = "AIza" + "Sy" * 17 + "Q"          # 35 chars after the prefix
TURN_RE = re.compile(r"^\[[^\]\n]*\] (USER|ASSISTANT): ", re.M)


def _load():
    spec = importlib.util.spec_from_file_location("ici_extract", SCRIPTS / "extract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _msg(role, content, ts, sidechain=False):
    return {"parentUuid": None, "isSidechain": sidechain, "cwd": "/Users/o/Projects/alpha",
            "gitBranch": "main", "type": role, "message": {"role": role, "content": content},
            "uuid": "u-" + ts, "timestamp": ts}


def _ts(i):
    return f"2026-08-01T10:{i // 60:02d}:{i % 60:02d}Z"


def _write(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def make_tree(root: Path) -> None:
    d = root / SLUG
    d.mkdir(parents=True)
    recs = [
        {"type": "ai-title", "aiTitle": "Redaction session", "sessionId": "s"},
        _msg("user", "Hello <system-reminder>SECRET HARNESS BLOCK\nmore</system-reminder> world\nsecond line",
             _ts(0)),
        _msg("assistant", [{"type": "text", "text": f"Use token {GHP} and key {AIZA} carefully."}], _ts(1)),
        _msg("user", "[watcher-ping]", _ts(2)),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t", "content": "TOOL OUTPUT MUST NOT APPEAR"}],
             _ts(3)),
        _msg("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "echo hidden"}}], _ts(4)),
        _msg("user", "<system-reminder>only noise</system-reminder>", _ts(5)),
        _msg("user", "Caveat: The messages below were generated while running local commands.", _ts(6)),
    ]
    # enough dialog to need several chunks at a 4,000-char cap
    for i in range(7, 47):
        role = "user" if i % 2 else "assistant"
        text = f"turn {i} " + ("x" * 400)
        recs.append(_msg(role, text if role == "user" else [{"type": "text", "text": text}], _ts(i)))
    _write(d / f"{S1}.jsonl", recs)
    _write(d / f"{S2}.jsonl", [
        _msg("user", "second session: " + "y" * 420, "2026-07-01T09:00:00Z"),
        _msg("assistant", [{"type": "text", "text": "ok"}], "2026-07-01T09:01:00Z"),
    ])
    _write(d / f"{S3}.jsonl", [
        _msg("user", "<system-reminder>only noise</system-reminder>", "2026-06-02T09:00:00Z"),
        _msg("user", "[watcher-ping]", "2026-06-02T09:01:00Z"),
    ])
    _write(d / f"{S4}.jsonl", [
        _msg("user", "hi", "2026-06-01T09:00:00Z"),
        _msg("assistant", [{"type": "text", "text": "short answer"}], "2026-06-01T09:01:00Z"),
    ])


class Base(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "projects"
        make_tree(self.root)
        self.out = self.tmp / "out"

    def _chunks(self, uuid):
        return sorted((self.out / "dumps" / SLUG).glob(f"{uuid}.*.txt"),
                      key=lambda p: int(p.name.split(".")[-2]))

    def _body(self, path):
        text = path.read_text()
        head, _blank, body = text.partition("\n\n")
        self.assertTrue(head.startswith("# claude-import · project"))
        return body


class TestCleaningAndRedaction(Base):
    def test_noise_stripped_and_secrets_redacted(self):
        c = self.m.extract(self.root, out_dir=self.out, session=S1[:8], max_chunk_chars=4000)
        self.assertEqual(c["extracted"], 1)
        self.assertGreaterEqual(c["redactions"], 1)
        text = "".join(self._body(p) for p in self._chunks(S1))
        self.assertIn("USER: Hello  world\nsecond line", text)
        self.assertNotIn("SECRET HARNESS", text)
        self.assertNotIn("watcher-ping", text)
        self.assertNotIn("only noise", text)
        self.assertNotIn("Caveat: The messages below", text)
        self.assertNotIn("TOOL OUTPUT", text)
        self.assertNotIn("echo hidden", text)
        self.assertNotIn(GHP, text)
        self.assertNotIn(AIZA, text)
        self.assertIn("[STORED-IN-KEYCHAIN-GitHub Token]", text)
        self.assertIn("[STORED-IN-KEYCHAIN-Google API Key]", text)
        self.assertIn("carefully.", text)
        raw = (self.root / SLUG / f"{S1}.jsonl").read_text()
        self.assertIn(GHP, raw)  # the source was not touched

    def test_chunks_split_only_at_turn_lines(self):
        self.m.extract(self.root, out_dir=self.out, session=S1[:8], max_chunk_chars=4000)
        chunks = self._chunks(S1)
        self.assertGreater(len(chunks), 2)
        turns_seen = 0
        for n, p in enumerate(chunks, start=1):
            text = p.read_text()
            self.assertLessEqual(len(text), 4000)
            self.assertIn(f"chunk {n}/{len(chunks)}", text.split("\n", 1)[0])
            body = self._body(p)
            self.assertTrue(TURN_RE.match(body), f"chunk {n} does not start at a turn line")
            turns_seen += len(TURN_RE.findall(body))
        self.assertEqual(turns_seen, 42)  # 2 kept from the head + 40 generated
        # every kept turn is intact: "turn N" and its payload live in the same chunk
        for p in chunks:
            for line in self._body(p).split("\n"):
                if line and TURN_RE.match(line) and "turn " in line:
                    self.assertTrue(line.endswith("x" * 400))

    def test_files_0600_in_0700_dirs(self):
        self.m.extract(self.root, out_dir=self.out, session=S1[:8], max_chunk_chars=4000)
        for d in (self.out, self.out / "dumps", self.out / "dumps" / SLUG):
            self.assertEqual(oct(os.stat(d).st_mode & 0o777), "0o700", d)
        for p in self._chunks(S1):
            self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600", p)

    def test_chunk_turns_unit(self):
        text = "[t] USER: a\ncont\n[t] ASSISTANT: b\n[t] USER: " + "c" * 5000 + "\n"
        chunks = self.m.chunk_turns(text, max_chars=1300)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], "[t] USER: a\ncont\n[t] ASSISTANT: b\n")
        self.assertTrue(chunks[1].startswith("[t] USER: ccc"))
        self.assertTrue(chunks[1].rstrip("\n").endswith(self.m.TRUNCATED_MARK))
        self.assertLessEqual(len(chunks[1]), 1300)


class TestStateAndSelection(Base):
    def test_new_skips_unchanged_and_picks_up_a_touched_file(self):
        c = self.m.extract(self.root, out_dir=self.out)
        self.assertEqual((c["extracted"], c["skipped_unchanged"], c["skipped_empty"]), (2, 0, 2))
        state = json.loads((self.out / "state.json").read_text())
        rec = state["sessions"][f"{SLUG}/{S1}"]
        for k in ("extracted_at", "extracted_mtime_ns", "extracted_size", "chunks", "chars"):
            self.assertIn(k, rec)
        c = self.m.extract(self.root, out_dir=self.out, new_only=True)
        self.assertEqual((c["extracted"], c["skipped_unchanged"], c["skipped_empty"]), (0, 4, 0))
        p = self.root / SLUG / f"{S2}.jsonl"
        t = time.time() + 100
        os.utime(p, (t, t))
        c = self.m.extract(self.root, out_dir=self.out, new_only=True)
        self.assertEqual((c["extracted"], c["skipped_unchanged"], c["skipped_empty"]), (1, 3, 0))
        status = json.loads((self.out / "status.json").read_text())
        self.assertEqual(status["phase"], "extracted")
        self.assertEqual((status["extracted"], status["skipped_empty"]), (1, 0))

    def test_session_prefix_and_unknown(self):
        c = self.m.extract(self.root, out_dir=self.out, session="bbbbbbbb")
        self.assertEqual(c["extracted"], 1)
        self.assertEqual(len(self._chunks(S2)), 1)
        self.assertEqual(self._chunks(S1), [])
        with self.assertRaises(SystemExit):
            self.m.extract(self.root, out_dir=self.out, session="zzzz")

    def test_budget_stops_after_newest_session(self):
        c = self.m.extract(self.root, out_dir=self.out, max_chars_total=10)
        self.assertEqual(c["extracted"], 1)
        self.assertTrue(c["budget_exhausted"])
        self.assertEqual(c["remaining"], 3)
        self.assertTrue(self._chunks(S1))      # newest (2026-08) first
        self.assertEqual(self._chunks(S2), [])

    def test_out_dir_inside_root_refused(self):
        with self.assertRaises(SystemExit):
            self.m.extract(self.root, out_dir=self.root / "out")

    def test_cli_json_counts_only(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(["--root", str(self.root), "--out-dir", str(self.out), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual((doc["extracted"], doc["skipped_empty"]), (2, 2))
        self.assertNotIn("Redaction session", buf.getvalue())
        self.assertNotIn(str(self.root), buf.getvalue())


class TestEmptySessions(Base):
    """The 2026-09-10 bug: `extracted 47` printed while 43 dump files existed —
    four never-answered sessions produced no chunk yet were counted, and one
    178-byte dialog got a useless summary."""

    def _rec(self, uuid):
        return json.loads((self.out / "state.json").read_text())["sessions"][f"{SLUG}/{uuid}"]

    def test_empty_and_tiny_sessions_are_skipped_not_extracted(self):
        c = self.m.extract(self.root, out_dir=self.out)
        self.assertEqual((c["extracted"], c["skipped_empty"], c["errors"]), (2, 2, 0))
        self.assertTrue(self._chunks(S1) and self._chunks(S2))
        self.assertEqual(self._chunks(S3), [])          # no assistant turn: no chunk at all
        self.assertEqual(self._chunks(S4), [])          # ~60 chars of dialog: under --min-chars
        for u in (S3, S4):
            rec = self._rec(u)
            self.assertTrue(rec["skipped_empty"])
            self.assertEqual(rec["chunks"], 0)
            for k in ("extracted_at", "extracted_mtime_ns", "extracted_size"):
                self.assertIn(k, rec)
        self.assertNotIn("skipped_empty", self._rec(S1))
        self.assertGreater(self._rec(S4)["chars"], 0)   # the tiny one had dialog, just not enough
        # extracted == dump files written; status.json says the same
        written = {p.name.split(".")[0] for p in (self.out / "dumps" / SLUG).glob("*.txt")}
        self.assertEqual(written, {S1, S2})
        status = json.loads((self.out / "status.json").read_text())
        self.assertEqual((status["extracted"], status["skipped_empty"]), (2, 2))

    def test_human_summary_names_the_skipped_count(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["--root", str(self.root), "--out-dir", str(self.out)])
        self.assertIn("extracted 2 sessions", buf.getvalue())
        self.assertIn("2 empty skipped", buf.getvalue())

    def test_min_chars_zero_keeps_the_tiny_session(self):
        c = self.m.extract(self.root, out_dir=self.out, min_chars=0)
        self.assertEqual((c["extracted"], c["skipped_empty"]), (3, 1))
        self.assertEqual(len(self._chunks(S4)), 1)
        self.assertEqual(self._chunks(S3), [])          # still nothing to write
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main(["--root", str(self.root), "--out-dir", str(self.out), "--min-chars", "0", "--json"])
        self.assertEqual(json.loads(buf.getvalue())["extracted"], 3)

    def test_new_rerun_leaves_skipped_sessions_alone_until_the_file_changes(self):
        self.m.extract(self.root, out_dir=self.out)
        c = self.m.extract(self.root, out_dir=self.out, new_only=True)
        self.assertEqual((c["extracted"], c["skipped_empty"], c["skipped_unchanged"]), (0, 0, 4))
        p = self.root / SLUG / f"{S4}.jsonl"
        t = time.time() + 100
        os.utime(p, (t, t))
        c = self.m.extract(self.root, out_dir=self.out, new_only=True)
        self.assertEqual((c["extracted"], c["skipped_empty"], c["skipped_unchanged"]), (0, 1, 3))
        self.assertEqual(self._chunks(S4), [])

    def test_a_session_that_turns_out_empty_loses_its_old_dumps(self):
        self.m.extract(self.root, out_dir=self.out, session=S4[:8], min_chars=0)
        self.assertEqual(len(self._chunks(S4)), 1)
        c = self.m.extract(self.root, out_dir=self.out, session=S4[:8])
        self.assertEqual((c["extracted"], c["skipped_empty"]), (0, 1))
        self.assertEqual(self._chunks(S4), [])
        self.assertTrue(self._rec(S4)["skipped_empty"])
        # and back again once it qualifies: the flag goes away with the dump written
        self.m.extract(self.root, out_dir=self.out, session=S4[:8], min_chars=0)
        self.assertNotIn("skipped_empty", self._rec(S4))
        self.assertEqual(len(self._chunks(S4)), 1)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

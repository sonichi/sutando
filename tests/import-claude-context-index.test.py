#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/index.py — the LLM-free index.

Fixture: two project slugs, three top-level sessions (one a sidechain), one
nested subagents/x/y.jsonl, repeated `ai-title` lines (the LAST wins), `cwd`
on message lines (recovered), a `summary` record, a `custom-title`. Pins the
counts (incl. `empty` = sessions with no assistant message and
`conversations` = sessions − empty, the number the owner is told), the
read-only contract (root listing + mtimes identical, --counts-only opens no
file and is a file count, --dry-run writes nothing, out-dir inside the root
refused) and the (mtime,size) `new` bookkeeping.

Run: python3 tests/import-claude-context-index.test.py
"""
from __future__ import annotations

import builtins
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")

SLUG_A = "-Users-o-Projects-alpha"
SLUG_B = "-Users-o-Projects-beta"
CWD_A = "/Users/o/Projects/alpha"
CWD_B = "/Users/o/Projects/beta"
A1 = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
A2 = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
B1 = "33333333-cccc-4ccc-8ccc-cccccccccccc"
A3 = "44444444-dddd-4ddd-8ddd-dddddddddddd"   # never answered (added by one test)


def _load():
    spec = importlib.util.spec_from_file_location("ici_index", SCRIPTS / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _msg(role, content, ts, cwd=CWD_A, sidechain=False, **extra):
    d = {"parentUuid": None, "isSidechain": sidechain, "userType": "external",
         "cwd": cwd, "sessionId": "s", "version": "2.1.0", "gitBranch": "main",
         "type": role, "message": {"role": role, "content": content},
         "uuid": "u-" + ts, "timestamp": ts}
    d.update(extra)
    return d


def _write(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def make_tree(root: Path) -> None:
    a = root / SLUG_A
    a.mkdir(parents=True)
    _write(a / f"{A1}.jsonl", [
        {"type": "mode", "mode": "default", "sessionId": "s"},
        {"type": "ai-title", "aiTitle": "First title", "sessionId": "s"},
        _msg("user", "<system-reminder>harness noise</system-reminder>Build the widget please",
             "2026-08-01T10:00:00Z"),
        _msg("assistant", [{"type": "text", "text": "Sure."},
                           {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}],
             "2026-08-01T10:01:00Z"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "big output " * 200}],
             "2026-08-01T10:02:00Z", toolUseResult={"stdout": "x"}),
        _msg("assistant", [{"type": "tool_use", "name": "Read", "input": {}}], "2026-08-01T10:03:00Z"),
        {"type": "ai-title", "aiTitle": "Widget build", "sessionId": "s"},
        {"type": "last-prompt", "lastPrompt": "ship it", "sessionId": "s"},
        {"type": "agent-name", "agentName": "sutando", "sessionId": "s"},
        {"type": "summary", "summary": "Built the widget", "leafUuid": "u-x"},
        _msg("user", "ship it", "2026-08-02T09:00:00Z"),
    ])
    # sidechain: the first message event carries isSidechain true
    _write(a / f"{A2}.jsonl", [
        {"type": "mode", "mode": "default", "sessionId": "s2"},
        _msg("user", "subagent task", "2026-08-03T10:00:00Z", sidechain=True),
        _msg("assistant", [{"type": "text", "text": "done"}], "2026-08-03T10:01:00Z", sidechain=True),
    ])
    sub = a / "subagents" / "x"
    sub.mkdir(parents=True)
    _write(sub / "y.jsonl", [_msg("user", "nested", "2026-08-04T10:00:00Z")])
    (a / "memory").mkdir()
    (a / "memory" / "MEMORY.md").write_text("- note\n")

    b = root / SLUG_B
    b.mkdir()
    first = _msg("user", "Plan the beta launch", "2026-07-20T08:00:00Z", cwd=CWD_B)
    del first["cwd"]          # cwd only on a LATER message line -> regex fallback
    del first["gitBranch"]
    # real shape: a big message object, then cwd/gitBranch at the END of the line
    later = {"parentUuid": "p", "isSidechain": False,
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Here is a plan. " * 400}]},
             "type": "assistant", "uuid": "u2", "timestamp": "2026-07-20T08:05:00Z",
             "cwd": CWD_B, "gitBranch": "main"}
    _write(b / f"{B1}.jsonl", [
        {"type": "custom-title", "title": "Beta launch", "sessionId": "s3"},
        {"type": "ai-title", "aiTitle": "AI title for beta", "sessionId": "s3"},
        first,
        later,
    ])


def _snapshot(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames):
            out.append(("d", os.path.relpath(os.path.join(dirpath, name), root)))
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            out.append(("f", os.path.relpath(p, root), st.st_mtime_ns, st.st_size))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "projects"
        make_tree(self.root)
        self.out = self.tmp / "out"

    def _session(self, res, slug, uuid):
        for s in res["index"]["projects"][slug]["sessions"]:
            if s["uuid"] == uuid:
                return s
        self.fail(f"{uuid} not indexed")


class TestFullPass(Base):
    def test_counts_last_title_wins_and_cwd_recovery(self):
        res = self.m.index(self.root, out_dir=self.out)
        self.assertEqual(res["projects"], 2)
        self.assertEqual(res["sessions"], 2)
        self.assertEqual(res["sidechains"], 1)
        self.assertEqual(res["subagent_files"], 1)
        self.assertEqual(res["new"], 2)
        self.assertEqual((res["empty"], res["conversations"]), (0, 2))
        self.assertEqual(res["earliest"], "2026-07-20T08:00:00Z")
        self.assertEqual(res["latest"], "2026-08-02T09:00:00Z")

        a1 = self._session(res, SLUG_A, A1)
        self.assertEqual(a1["title"], "Widget build")      # last ai-title, not the first
        self.assertEqual(a1["title_source"], "ai")
        self.assertEqual(a1["cwd"], CWD_A)
        self.assertEqual(a1["git_branch"], "main")
        self.assertEqual(a1["first_ts"], "2026-08-01T10:00:00Z")
        self.assertEqual(a1["last_ts"], "2026-08-02T09:00:00Z")
        self.assertEqual(a1["user_msgs"], 2)               # tool_result echo excluded
        self.assertEqual(a1["assistant_msgs"], 1)          # tool-only assistant line excluded
        self.assertEqual(a1["tool_lines"], 2)
        self.assertEqual(a1["last_prompt"], "ship it")
        self.assertEqual(a1["agent_name"], "sutando")
        self.assertEqual(a1["summary"], "Built the widget")
        self.assertEqual(a1["first_prompt"], "Build the widget please")  # noise stripped
        self.assertEqual(res["index"]["projects"][SLUG_A]["cwd"], CWD_A)

        b1 = self._session(res, SLUG_B, B1)
        self.assertEqual(b1["title"], "Beta launch")       # custom-title beats ai-title
        self.assertEqual(b1["cwd"], CWD_B)                 # recovered from a later line
        self.assertEqual(b1["git_branch"], "main")

        # outputs
        self.assertTrue((self.out / "index.json").is_file())
        md = (self.out / "claude-import-index.md").read_text()
        self.assertIn("| [ ] | 2026-08-01 | 11111111 | Widget build | 2/1 |", md)
        self.assertIn(f"- cwd: `{CWD_A}`", md)
        self.assertIn(f"## {SLUG_B}", md)
        self.assertNotIn("subagent task", md)              # the sidechain is not listed
        self.assertEqual(oct(os.stat(self.out / "index.json").st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.out).st_mode & 0o777), "0o700")
        status = json.loads((self.out / "status.json").read_text())
        self.assertEqual(status["phase"], "indexed")
        self.assertEqual(status["sessions"], 2)
        self.assertEqual(set(status) - {"phase", "updated_at"},
                         {"sessions", "projects", "conversations", "empty", "new", "sidechains",
                          "subagent_files"})
        self.assertEqual((status["conversations"], status["empty"]), (2, 0))

    def test_empty_sessions_are_counted_and_left_out_of_conversations(self):
        # an aborted session: the owner typed, nothing ever answered (assistant_msgs 0)
        _write(self.root / SLUG_A / f"{A3}.jsonl", [
            _msg("user", "are you there?", "2026-08-07T10:00:00Z"),
            _msg("user", "<system-reminder>noise</system-reminder>", "2026-08-07T10:01:00Z"),
        ])
        res = self.m.index(self.root, out_dir=self.out)
        self.assertEqual((res["sessions"], res["empty"], res["conversations"]), (3, 1, 2))
        self.assertEqual(self._session(res, SLUG_A, A3)["assistant_msgs"], 0)
        self.assertEqual(res["index"]["projects"][SLUG_A]["empty"], 1)
        self.assertEqual(res["index"]["projects"][SLUG_B]["empty"], 0)
        self.assertEqual(res["index"]["counts"]["conversations"], 2)
        status = json.loads((self.out / "status.json").read_text())
        self.assertEqual((status["sessions"], status["conversations"], status["empty"]), (3, 2, 1))
        md = (self.out / "claude-import-index.md").read_text()
        self.assertIn("3 sessions across 2 projects — 2 conversations, 1 with no assistant reply", md)
        # --counts-only is a file count: it cannot know
        res = self.m.index(self.root, out_dir=self.out, counts_only=True)
        self.assertEqual(res["sessions"], 4)
        self.assertIsNone(res["empty"])
        self.assertIsNone(res["conversations"])

    def test_real_key_order_message_object_before_type(self):
        # Claude Code writes assistant lines with the API message object (which
        # itself carries "type":"message") BEFORE the record's own type key.
        real = ('{"parentUuid":"p","isSidechain":false,"cwd":"%s","sessionId":"s",'
                '"message":{"id":"m","type":"message","role":"assistant","model":"x",'
                '"content":[{"type":"text","text":"real answer"}],"stop_reason":"end_turn"},'
                '"type":"assistant","uuid":"u9","timestamp":"2026-08-06T10:00:00Z"}\n' % CWD_A)
        p = self.root / SLUG_A / f"{A1}.jsonl"
        with open(p, "a") as fh:
            fh.write(real)
            fh.write('{"type":"user","message":{"role":"user","content":'
                     '"literal \\"type\\":\\"assistant\\" inside text"},'
                     '"timestamp":"2026-08-06T10:01:00Z","isSidechain":false}\n')
        res = self.m.index(self.root, out_dir=self.out, dry_run=True)
        a1 = self._session(res, SLUG_A, A1)
        self.assertEqual(a1["assistant_msgs"], 2)
        self.assertEqual(a1["user_msgs"], 3)
        self.assertEqual(a1["last_ts"], "2026-08-06T10:01:00Z")

    def test_root_listing_and_mtimes_identical(self):
        before = _snapshot(self.root)
        self.m.index(self.root, out_dir=self.out)
        self.m.index(self.root, out_dir=self.out, counts_only=True)
        self.m.index(self.root, out_dir=self.out, dry_run=True)
        self.assertEqual(_snapshot(self.root), before)

    def test_new_is_zero_on_rerun_and_one_after_touch(self):
        self.assertEqual(self.m.index(self.root, out_dir=self.out)["new"], 2)
        again = self.m.index(self.root, out_dir=self.out, new_only=True)
        self.assertEqual(again["new"], 0)
        self.assertEqual(again["session_keys"], [])
        p = self.root / SLUG_A / f"{A1}.jsonl"
        t = time.time() + 100
        os.utime(p, (t, t))
        third = self.m.index(self.root, out_dir=self.out, new_only=True)
        self.assertEqual(third["new"], 1)
        self.assertEqual(third["session_keys"], [f"{SLUG_A}/{A1}"])

    def test_projects_filter_merges_over_previous_index(self):
        self.m.index(self.root, out_dir=self.out)
        res = self.m.index(self.root, out_dir=self.out, projects="beta")
        self.assertEqual(res["projects"], 1)
        doc = json.loads((self.out / "index.json").read_text())
        self.assertEqual(set(doc["projects"]), {SLUG_A, SLUG_B})

    def test_since_filters_on_mtime_before_any_read(self):
        old = time.time() - 10 * 86400
        for name in (f"{A1}.jsonl", f"{A2}.jsonl"):
            os.utime(self.root / SLUG_A / name, (old, old))
        res = self.m.index(self.root, out_dir=self.out, dry_run=True, since="1d")
        self.assertEqual(res["sessions"], 1)
        self.assertEqual(res["skipped_since"], 2)
        self.assertEqual(res["sidechains"], 0)


class TestReadOnlyContract(Base):
    def test_counts_only_opens_no_file_and_writes_nothing(self):
        def boom(*a, **k):
            raise AssertionError(f"open() called in --counts-only: {a[:1]}")
        with patch.object(builtins, "open", boom), patch.object(io, "open", boom):
            res = self.m.index(self.root, out_dir=self.out, counts_only=True)
        self.assertTrue(res["counts_only"])
        self.assertEqual(res["projects"], 2)
        self.assertEqual(res["sessions"], 3)          # stat-level: the sidechain cannot be told apart
        self.assertEqual(res["subagent_files"], 1)
        self.assertIsNone(res["sidechains"])
        self.assertIsNone(res["new"])
        self.assertIsNone(res["dialog_bytes"])
        self.assertIsNone(res["conversations"])
        self.assertGreater(res["bytes_on_disk"], 0)
        self.assertFalse(self.out.exists())

    def test_dry_run_writes_nothing(self):
        res = self.m.index(self.root, out_dir=self.out, dry_run=True)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["sessions"], 2)
        self.assertFalse(self.out.exists())
        self.assertEqual(res["index"]["counts"]["sessions"], 2)

    def test_out_dir_inside_root_refused(self):
        with self.assertRaises(SystemExit):
            self.m.index(self.root, out_dir=self.root / "import-out")
        with self.assertRaises(SystemExit):
            self.m.index(self.root, out_dir=self.root)
        with self.assertRaises(SystemExit):
            self.m.index(self.root, out_dir=self.root / SLUG_A / "memory")
        self.assertFalse((self.root / "import-out").exists())

    def test_missing_root_is_a_clean_refusal(self):
        with self.assertRaises(SystemExit):
            self.m.index(self.tmp / "nope", out_dir=self.out)


class TestCli(Base):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main(argv)
        return rc, buf.getvalue()

    def test_json_prints_counts_only(self):
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out), "--dry-run", "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual(doc["sessions"], 2)
        self.assertEqual(doc["projects"], 2)
        self.assertEqual(set(doc), set(self.m.COUNT_KEYS))
        for leak in ("Widget build", "Beta launch", "Build the widget", str(self.root), SLUG_A, CWD_A):
            self.assertNotIn(leak, out)
        self.assertFalse(self.out.exists())

    def test_projects_flag_takes_a_dash_leading_slug(self):
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out), "--dry-run",
                             "--json", "--projects", SLUG_B])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["sessions"], 1)
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out), "--dry-run",
                             "--json", f"--projects={SLUG_A},beta"])
        self.assertEqual(json.loads(out)["sessions"], 2)

    def test_counts_only_json_and_human_line(self):
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out), "--counts-only", "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(out)
        self.assertEqual((doc["sessions"], doc["projects"], doc["subagent_files"]), (3, 2, 1))
        self.assertTrue(doc["counts_only"])
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out), "--counts-only"])
        self.assertIn("3 transcripts across 2 projects", out)
        self.assertIn("a file count", out)
        _write(self.root / SLUG_A / f"{A3}.jsonl", [_msg("user", "hello?", "2026-08-07T10:00:00Z")])
        rc, out = self._run(["--root", str(self.root), "--out-dir", str(self.out)])
        self.assertIn("3 sessions across 2 projects (2 conversations, 1 empty;", out)
        self.assertNotIn("Widget", out)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

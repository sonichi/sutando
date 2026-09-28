#!/usr/bin/env python3
"""The import digest reaches the owner in parts the AG2 Space bridge can carry.

The bridge dead-letters a proactive body over 48 KiB (remote_gateway_bridge
_PROACTIVE_MAX_BODY_B) without delivering it, so a digest of a normal Claude
Code history (dozens of projects) never arrived. finalize.py --digest-files
now cuts review.md into ordered parts under that cap.

Run: python3 tests/import-claude-context-digest-parts.test.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
BRIDGE_CAP = 48 * 1024


def _load():
    spec = importlib.util.spec_from_file_location("ici_finalize_digest", SCRIPTS / "finalize.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


F = _load()


def _big_review(projects: int = 40) -> str:
    head = ("# Claude Code import — review before it lands\n\nStaged today (user run): 400 sessions "
            "across %d projects. Nothing below has been written yet.\n\n## Projects\n\n" % projects)
    body = ""
    for i in range(projects):
        body += f"### Project {i} (`proj-{i}`)\n\n"
        body += "dir `/Users/someone/code/project-%d` · 10 sessions (2026-01-01 → 2026-02-01) · status: active\n\n" % i
        body += ("A paragraph about the project — what it does, why it matters, and the decisions taken so far; "
                 "it runs long because roll-ups are prose, not bullets. " * 6).strip() + "\n\n"
        body += "**Open threads**\n" + "".join(f"- thread {j}: {'x' * 150} — because {'y' * 100}\n" for j in range(12))
        body += "\n**Decisions**\n" + "".join(f"- decision {j}: {'z' * 150} — because {'w' * 100}\n" for j in range(12))
        body += "\n```python\nprint('a fence that must not be split across parts')\n```\n\n"
    people = "## People\n\nAlready in your People store — will gain new interactions (200):\n"
    people += "".join(f"- Person Number {i} — matched on email (3 citations)\n" for i in range(200))
    tail = "\n## Memory\n\n1,900 B summary.\n\n"
    return head + body + people + tail + f"\n---\n{F.REVIEW_FOOTER}\n"


class DigestParts(unittest.TestCase):
    def test_a_big_digest_is_cut_into_deliverable_ordered_parts(self):
        text = _big_review()
        self.assertGreater(len(text.encode("utf-8")), BRIDGE_CAP, "the fixture must exceed the bridge cap")
        parts = F.digest_parts(text)
        self.assertGreater(len(parts), 1)
        n = len(parts)
        for i, part in enumerate(parts, 1):
            self.assertLessEqual(len(part.encode("utf-8")), BRIDGE_CAP, f"part {i} would be dead-lettered")
            self.assertTrue(part.startswith(f"Claude Code import — part {i}/{n}\n\n"), part[:60])
            self.assertNotIn("[file:", part)
        joined = "".join(parts)
        for i in range(40):
            self.assertIn(f"### Project {i} (`proj-{i}`)", joined)
        self.assertEqual(sum(F.REVIEW_FOOTER in p for p in parts), 1, "the reply footer appears once")
        self.assertIn(F.REVIEW_FOOTER, parts[-1], "…on the last part")
        # A fence opened in one part is closed there and reopened in the next (chunk_message's rule).
        for part in parts:
            self.assertEqual(part.count("```") % 2, 0, "unbalanced code fence inside a part")

    def test_a_small_digest_is_one_file_verbatim(self):
        text = "# Claude Code import — review before it lands\n\nshort\n" + f"\n---\n{F.REVIEW_FOOTER}\n"
        self.assertEqual(F.digest_parts(text), [text])

    def test_multibyte_text_stays_under_the_cap(self):
        text = ("### 项目 (`p`)\n\n" + ("决策 — 因为 → 结果 … " * 2000) + "\n") * 3 + f"\n---\n{F.REVIEW_FOOTER}\n"
        parts = F.digest_parts(text)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), BRIDGE_CAP)

    def test_digest_files_writes_parts_that_sort_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "staged").mkdir(parents=True)
            (data / "staged" / "review.md").write_text(_big_review(), encoding="utf-8")
            results = Path(td) / "results"
            r = F.write_digest_files(data_dir=data, results_dir=results)
            files = sorted(p.name for p in results.iterdir())
            self.assertEqual(files, r["files"])
            self.assertEqual(len(files), r["parts"])
            self.assertTrue(all(re.fullmatch(r"proactive-\d+-\d{2}\.txt", f) for f in files), files)
            self.assertFalse(list(results.glob(".*.tmp")), "no half-written file is left behind")
            self.assertLessEqual(r["largest_part_bytes"], BRIDGE_CAP)
            for i, f in enumerate(files, 1):
                self.assertIn(f"part {i}/{len(files)}", (results / f).read_text(encoding="utf-8"))

    def test_digest_files_single_part_keeps_the_plain_name(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "staged").mkdir(parents=True)
            (data / "staged" / "review.md").write_text("# small\n\nok\n", encoding="utf-8")
            r = F.write_digest_files(data_dir=data, results_dir=Path(td) / "results")
            self.assertEqual(r["parts"], 1)
            self.assertTrue(re.fullmatch(r"proactive-\d+\.txt", r["files"][0]), r["files"])

    def test_cli_reports_json(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "staged").mkdir(parents=True)
            (data / "staged" / "review.md").write_text(_big_review(), encoding="utf-8")
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "finalize.py"), "--digest-files", "--json",
                 "--workspace", td, "--data-dir", str(data), "--results-dir", str(Path(td) / "results"),
                 "--memory-dir", str(Path(td) / "mem")],
                capture_output=True, text=True, check=False)
            self.assertEqual(out.returncode, 0, out.stderr[-800:])
            r = json.loads(out.stdout)
            self.assertGreater(r["parts"], 1)

    def test_main_digest_files_in_process_json_and_plain(self):
        # In-process (coverage sees it): the --digest-files branch of main, both report shapes,
        # with --results-dir given and defaulted to <workspace>/results.
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "staged").mkdir(parents=True)
            (data / "staged" / "review.md").write_text(_big_review(), encoding="utf-8")
            common = ["--digest-files", "--workspace", td, "--data-dir", str(data),
                      "--memory-dir", str(Path(td) / "mem")]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = F.main(common + ["--json", "--results-dir", str(Path(td) / "r1")])
            self.assertEqual(rc, 0)
            r = json.loads(out.getvalue())
            self.assertGreater(r["parts"], 1)
            self.assertEqual(sorted(p.name for p in (Path(td) / "r1").iterdir()), r["files"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = F.main(common)
            self.assertEqual(rc, 0)
            line = out.getvalue()
            self.assertRegex(line, r"^posted the digest as \d+ part\(s\) \(\d+ B\): proactive-")
            self.assertTrue((Path(td) / "results").is_dir(), "defaults to <workspace>/results")

    def test_digest_files_refuses_when_nothing_is_staged(self):
        with tempfile.TemporaryDirectory() as td:
            data = Path(td) / "data"
            (data / "staged").mkdir(parents=True)
            with self.assertRaises(SystemExit) as cm:
                F.write_digest_files(data_dir=data, results_dir=Path(td) / "results")
            self.assertIn("nothing is staged", str(cm.exception))
            self.assertFalse((Path(td) / "results").exists(), "nothing is written")

    def test_the_known_people_list_is_capped(self):
        cands = [({"name": f"P{i}"}, ["c1", "c2"], {"matched_on": "email"}) for i in range(80)]
        text = F._people_section(cands, [], 0, {}, True)
        self.assertEqual(text.count("- P"), F.PEOPLE_LIST_SHOWN)
        self.assertIn(f"…and {80 - F.PEOPLE_LIST_SHOWN} more", text)


if __name__ == "__main__":
    unittest.main()

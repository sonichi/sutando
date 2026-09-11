#!/usr/bin/env python3
"""Tests for skills/import-claude-context/scripts/_common.py — the shared plumbing.

Pins: workspace resolution (--workspace wins and never runs the config helper;
otherwise the helper's stdout, an empty answer refused), the read-only-root
guard including a cross-drive comparison, private (0600) JSON writes,
status.json taking counts only, --since parsing, --projects matching, the
dash-leading slug rewrite (from an explicit argv and from sys.argv) and
`redact_text`, the one redaction policy extract.py and index.py share.

Run: python3 tests/import-claude-context-common.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "import-claude-context" / "scripts"
os.environ.setdefault("SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER", "1")


def _load():
    spec = importlib.util.spec_from_file_location("ici_common", SCRIPTS / "_common.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCommon(unittest.TestCase):
    def setUp(self):
        self.m = _load()
        self.tmp = Path(tempfile.mkdtemp())

    def test_explicit_workspace_wins_without_the_helper(self):
        with patch.object(self.m.subprocess, "run", side_effect=AssertionError("must not run")):
            self.assertEqual(self.m.workspace_root(str(self.tmp)), self.tmp.resolve())
            self.assertEqual(self.m.data_dir(explicit=str(self.tmp / "d")), (self.tmp / "d").resolve())
            self.assertEqual(self.m.data_dir(str(self.tmp)), self.tmp.resolve() / "data" / "claude-import")

    def test_workspace_from_the_config_helper(self):
        def fake(cmd, **_kw):
            self.assertEqual((cmd[0], cmd[2]), ("bash", "workspace"))
            self.assertTrue(cmd[1].endswith("sutando-config.sh"))
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.tmp}\n", stderr="")
        with patch.object(self.m.subprocess, "run", side_effect=fake):
            self.assertEqual(self.m.workspace_root(None), self.tmp)
            self.assertEqual(self.m.data_dir(), self.tmp / "data" / "claude-import")
        empty = subprocess.CompletedProcess([], 0, stdout="\n", stderr="")
        with patch.object(self.m.subprocess, "run", return_value=empty):
            with self.assertRaises(SystemExit):
                self.m.workspace_root(None)

    def test_refuse_inside(self):
        root = self.tmp / "root"
        root.mkdir()
        with self.assertRaises(SystemExit):
            self.m.refuse_inside(root, root / "out")
        with self.assertRaises(SystemExit):
            self.m.refuse_inside(root, root)
        self.m.refuse_inside(root, self.tmp / "out")   # a sibling is fine
        with patch.object(self.m.os.path, "commonpath", side_effect=ValueError("different drives")):
            self.m.refuse_inside(root, root / "out")   # no common path at all: cannot be inside

    def test_json_helpers_and_private_writes(self):
        p = self.tmp / "x.json"
        self.m.write_json(p, {"a": 1}, private=True)
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        self.m.write_json(self.tmp / "y.json", [1])
        self.assertEqual(self.m.load_json(self.tmp / "y.json", None), [1])
        self.assertEqual(self.m.load_json(self.tmp / "missing.json", "dflt"), "dflt")
        (self.tmp / "bad.json").write_text("{not json")
        self.assertEqual(self.m.load_json(self.tmp / "bad.json", 7), 7)
        self.assertEqual(self.m.dump_json({"b": 1, "a": "é"}), '{\n  "a": "é",\n  "b": 1\n}\n')
        d = self.m.ensure_private_dir(self.tmp / "p" / "q")
        self.assertEqual(oct(os.stat(d).st_mode & 0o777), "0o700")

    def test_status_takes_counts_only(self):
        s = self.m.write_status(self.tmp, "indexed", sessions=3, done=True, new=None)
        self.assertEqual((s["phase"], s["sessions"], s["done"], s["new"]), ("indexed", 3, True, None))
        self.assertEqual(json.loads((self.tmp / "status.json").read_text())["sessions"], 3)
        with self.assertRaises(ValueError):
            self.m.write_status(self.tmp, "indexed", title="Widget build")
        self.assertEqual(json.loads((self.tmp / "status.json").read_text())["phase"], "indexed")

    def test_parse_since(self):
        self.assertIsNone(self.m.parse_since(None))
        self.assertIsNone(self.m.parse_since(""))
        now = time.time()
        self.assertAlmostEqual(self.m.parse_since("2d"), now - 2 * 86400, delta=5)
        self.assertAlmostEqual(self.m.parse_since(" 3h "), now - 3 * 3600, delta=5)
        self.assertAlmostEqual(self.m.parse_since("1w"), now - 7 * 86400, delta=5)
        self.assertEqual(self.m.parse_since("1970-01-02"), 86400.0)
        with self.assertRaises(SystemExit):
            self.m.parse_since("yesterday")

    def test_matches_project_and_split_csv(self):
        slug = "-Users-o-Projects-gtm"
        self.assertTrue(self.m.matches_project(slug, None))
        self.assertTrue(self.m.matches_project(slug, []))
        self.assertTrue(self.m.matches_project(slug, ["GTM"]))
        self.assertTrue(self.m.matches_project(slug, [slug]))
        self.assertFalse(self.m.matches_project(slug, ["alpha", ""]))
        self.assertEqual(self.m.split_csv(" a, ,b "), ["a", "b"])
        self.assertEqual(self.m.split_csv(None), [])

    def test_absorb_dash_values(self):
        flags = ("--projects", "--forget")
        self.assertEqual(self.m.absorb_dash_values(["--projects", "-Users-x", "--json"], flags),
                         ["--projects=-Users-x", "--json"])
        self.assertEqual(self.m.absorb_dash_values(["--projects", "--json"], flags), ["--projects", "--json"])
        self.assertEqual(self.m.absorb_dash_values(["--forget"], flags), ["--forget"])
        self.assertEqual(self.m.absorb_dash_values(["--since", "-1d"], flags), ["--since", "-1d"])
        with patch.object(self.m.sys, "argv", ["finalize.py", "--forget", "-Users-y"]):
            self.assertEqual(self.m.absorb_dash_values(None, flags), ["--forget=-Users-y"])

    def test_state_round_trip_and_stamps(self):
        st = self.m.load_state(self.tmp)
        self.assertEqual(st, {"sessions": {}, "projects": {}})
        st["sessions"][self.m.session_key("s", "u")] = {"x": 1}
        self.m.save_state(self.tmp, st)
        again = self.m.load_state(self.tmp)
        self.assertEqual(again["sessions"], {"s/u": {"x": 1}})
        self.assertIn("updated_at", again)
        self.assertRegex(self.m.now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(self.m.today(), r"^\d{4}-\d{2}-\d{2}$")

    def test_redact_text_is_the_importers_one_policy(self):
        ghp = "ghp_" + "A1b2C3d4" * 5
        aiza = "AIza" + "Sy" * 17 + "Q"
        ant = "sk-ant-api03-" + "Ab1_" * 23 + "x"
        n, out = self.m.redact_text(f"token {ghp}, key {aiza}, ANTHROPIC_API_KEY={ant} done")
        self.assertGreaterEqual(n, 3)
        for leak in (ghp, aiza, ant, "sk-ant-"):
            self.assertNotIn(leak, out)
        self.assertEqual(out, "token [STORED-IN-KEYCHAIN-GitHub Token], key [STORED-IN-KEYCHAIN-Google API Key], "
                              "ANTHROPIC_API_KEY=[STORED-IN-KEYCHAIN-Anthropic API Key] done")
        self.assertEqual(self.m.redact_text("nothing secret here"), (0, "nothing secret here"))
        self.assertEqual(set(self.m.EXTRA_SECRET_PATTERNS), {"Google API Key", "Anthropic API Key"})


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.exit(0 if result.wasSuccessful() else 1)

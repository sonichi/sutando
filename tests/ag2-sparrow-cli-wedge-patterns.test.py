#!/usr/bin/env python3
"""cli_wedge's abnormal-banner table is the data module cli_wedge_patterns, importable without the
detector and vendored byte-identical into ag2-sparrow: the detector's names are the
module's own objects, the grammar matches the banners it names, and the vendored copy imports alone."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "ag2-sparrow"
sys.path.insert(0, str(REPO / "src"))
import cli_wedge  # noqa: E402
import cli_wedge_patterns as patterns  # noqa: E402

SAMPLES = {
    "quota-limit": "You've hit your usage limit · resets 3pm",
    "out-of-credits": "Credit balance is too low",
    "needs-login": "OAuth token has expired · Please run /login",
    "compacting": "Compacting conversation…",
    "awaiting-input": "Waiting for your approval",
    "api-error": "API Error: 500 internal server error",
    "network-error": "fetch failed: ECONNREFUSED",
}


class Patterns(unittest.TestCase):
    def test_the_detector_reads_the_table_from_the_data_module(self):
        self.assertIs(cli_wedge.ABNORMAL_PATTERNS, patterns.ABNORMAL_PATTERNS)
        self.assertIs(cli_wedge.PROVIDER_LIMIT_PATTERNS, patterns.PROVIDER_LIMIT_PATTERNS)
        self.assertIs(cli_wedge._AUTH_REJECTED, patterns._AUTH_REJECTED)
        source = (REPO / "src" / "cli_wedge.py").read_text()
        self.assertNotIn("ABNORMAL_PATTERNS: tuple", source, "one definition, in the data module")

    def test_the_grammar_names_each_sample_banner(self):
        names = [n for n, _ in patterns.ABNORMAL_PATTERNS]
        self.assertEqual(names, list(SAMPLES))
        for name, line in SAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual([n for n, rx in patterns.ABNORMAL_PATTERNS if rx.search(line)][0], name)
        self.assertEqual(patterns.PROVIDER_LIMIT_PATTERNS, ("quota-limit", "out-of-credits"))
        self.assertFalse([n for n, rx in patterns.ABNORMAL_PATTERNS if rx.search("Reading src/foo.py")])

    def test_vendored_byte_identical_and_standalone(self):
        self.assertIn('"src/cli_wedge_patterns.py": "cli_wedge_patterns.py"', (PKG / "tools" / "sync_from_src.py").read_text())
        self.assertEqual((REPO / "src" / "cli_wedge_patterns.py").read_bytes(),
                         (PKG / "ag2_sparrow" / "cli_wedge_patterns.py").read_bytes())
        code = ("import sys; sys.path.insert(0, %r)\nfrom ag2_sparrow import cli_wedge_patterns as p\n"
                "assert 'cli_wedge' not in sys.modules and len(p.ABNORMAL_PATTERNS) == 7\nprint('OK')" % str(PKG))
        with tempfile.TemporaryDirectory() as cwd:
            r = subprocess.run([sys.executable, "-I", "-c", code], cwd=cwd, capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "OK"), r.stderr)


if __name__ == "__main__":
    unittest.main()

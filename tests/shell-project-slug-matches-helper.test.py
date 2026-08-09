#!/usr/bin/env python3
"""Shell slug derivations must match claude_project_slug(). A slash-only
derivation resolves to a directory Claude Code never creates on a path with a
space or dot, which is exactly the bundled `Application Support/space.ag2.app`
install this centralization exists to fix."""
from __future__ import annotations

import importlib.util
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL_IDIOM = "tr -c 'A-Za-z0-9' '-'"
# Every shell site that derives a Claude Code project slug.
SITES = [
    "src/agent/claude/cli/sutando-shell-setup.sh",
    "scripts/sync-memory.sh",
    "scripts/sync-workspace.sh",
]
PATHS = [
    "/Users/x/Library/Application Support/space.ag2.app/sutando",
    "/Users/x/stando-ui/sutando",
    "/Users/x/repo.with.dots/a b c",
]


def _helper():
    spec = importlib.util.spec_from_file_location("up", REPO / "src" / "util_paths.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.claude_project_slug


class TestShellSlugMatchesHelper(unittest.TestCase):
    def test_the_shell_idiom_produces_the_same_slug_as_the_helper(self):
        # Executed, not asserted from reading: run the real pipeline.
        slug = _helper()
        for p in PATHS:
            out = subprocess.run(["sh", "-c", f"printf '%s' \"$1\" | {SHELL_IDIOM}", "sh", p],
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual(slug(p), out, f"shell idiom diverged from the helper on {p!r}")

    def test_a_slash_only_derivation_would_be_caught(self):
        # Control: the OLD idiom must actually disagree, else this suite proves
        # nothing about the fix.
        slug = _helper()
        bad = "/Users/x/Library/Application Support/space.ag2.app/sutando"
        out = subprocess.run(["sh", "-c", "printf '%s' \"$1\" | tr '/' '-'", "sh", bad],
                             capture_output=True, text=True, check=True).stdout
        self.assertNotEqual(slug(bad), out, "the slash-only form must differ, or the control is dead")

    def test_every_shell_site_uses_the_complement_idiom(self):
        for rel in SITES:
            # `in`, not assertIn: these files are up to 90KB and assertIn dumps
            # the whole body into the failure, burying the message.
            body = (REPO / rel).read_text()
            self.assertTrue(SHELL_IDIOM in body,
                            f"{rel} must derive the slug with the complement set")

    def test_no_shell_file_derives_a_slug_with_a_slash_only_replacement(self):
        slash_only = re.compile(r"tr\s+'/'\s+'-'|sed\s+'s\|/\|-\|g'")
        offenders = []
        for d in ("src", "scripts"):
            for f in sorted((REPO / d).rglob("*.sh")):
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if slash_only.search(line):
                        offenders.append(f"{f.relative_to(REPO)}:{i}")
        self.assertEqual([], offenders,
                         "slash-only slug derivation(s) reintroduced: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()

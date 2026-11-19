#!/usr/bin/env python3
"""The standalone overview is a safe, self-contained companion to the owner tour."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

PAGE = Path(__file__).resolve().parent.parent / "docs" / "getting-started.html"


class OverviewTest(unittest.TestCase):
    def test_four_steps_and_demo_boundary(self):
        page = PAGE.read_text()
        self.assertEqual(re.findall(r'id="step-(\d)"', page), ["0", "1", "2", "3"])
        self.assertIn("do not connect apps, change permissions or start calls", page)
        self.assertNotIn("Spacer", page)
        self.assertNotRegex(page, r'<script[^>]+src=|@font-face|fetch\(')

    def test_accessibility_and_clipboard_fallback(self):
        page = PAGE.read_text()
        for value in ("min-height:44px", "prefers-reduced-motion", "<noscript>",
                      "aria-current", "heading.focus()", "selection.addRange(range)",
                      "try { await navigator.clipboard.writeText(text)"):
            self.assertIn(value, page)

    def test_content_boundaries(self):
        page = PAGE.read_text()
        for expected in ("Hermes", "OpenClaw", "Grok Bot", "Team — collaborator",
                         "Guest — owner approves first", "Bring into call",
                         "A schedule is not a Commons page", "setCommons('board'"):
            self.assertIn(expected, page)
        for obsolete in ("Gemini (Google)", "Anthropic Code / Duet",
                         "setCommons('cron'", "logs on my screen", "Share any window"):
            self.assertNotIn(obsolete, page)

    def test_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node unavailable")
        script = "\n".join(re.findall(r'<script>(.*?)</script>', PAGE.read_text(), re.S))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "overview.js"
            target.write_text(script)
            result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

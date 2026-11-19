#!/usr/bin/env python3
"""The standalone overview is a safe, self-contained companion to the owner tour."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

PAGE = Path(__file__).resolve().parent.parent / "docs" / "getting-started.html"


class OverviewTest(unittest.TestCase):
    def test_four_steps_and_demo_boundary(self):
        # Emitted copy and demo boundaries are intentional data pins.
        page = PAGE.read_text()
        self.assertEqual(re.findall(r'id="step-(\d)"', page), ["0", "1", "2", "3"])
        self.assertIn("do not connect apps, change permissions or start calls", page)
        self.assertNotIn("Spacer", page)
        self.assertNotRegex(page, r'<script[^>]+src=|@font-face|fetch\(')

    def test_accessibility_markup_and_css_data_pins(self):
        page = PAGE.read_text()
        for value in ("min-height:44px", "prefers-reduced-motion", "<noscript>",
                      "aria-current"):
            self.assertIn(value, page)

    def test_content_boundaries(self):
        # Emitted product copy is pinned independently of runtime behaviour.
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


class OverviewBehaviourTest(unittest.TestCase):
    def test_denied_clipboard_selects_prompt_and_next_focuses_heading(self):
        node = shutil.which("node")
        if not node or subprocess.run([node, "-e", "require('jsdom')"],
                                      capture_output=True).returncode:
            self.skipTest("node with jsdom not available")
        harness = r"""
const { JSDOM } = require('jsdom');
const html = require('fs').readFileSync(process.argv[2], 'utf8');
const errors = [];
let copies = 0;
const dom = new JSDOM(html, { url: 'https://page.test/', runScripts: 'dangerously',
  beforeParse(w) {
    w.addEventListener('error', e => errors.push(String(e.message)));
    // jsdom has no layout-backed innerText; these prompts contain plain text only.
    Object.defineProperty(w.HTMLElement.prototype, 'innerText', {
      get() { return this.textContent; }, set(v) { this.textContent = v; }
    });
    Object.defineProperty(w.navigator, 'clipboard', { value: {
      writeText: () => { copies++; return Promise.reject(new Error('denied')); }
    }});
  }
});
(async () => {
  const d = dom.window.document;
  const button = d.querySelector('#step-0 .btn-copy');
  button.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const result = { copies, selected: dom.window.getSelection().toString(),
    prompt: d.getElementById('prompt-p1').textContent, status: button.textContent };
  d.querySelector('#step-0 .btn-nav.primary').click();
  result.focusedHeading = d.activeElement === d.querySelector('#step-1 h2');
  result.activeStep = d.querySelector('.tour-card.active').id;
  result.errors = errors;
  console.log(JSON.stringify(result));
  dom.window.close();
})();
"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "overview.cjs"
            target.write_text(harness)
            result = subprocess.run([node, str(target), str(PAGE)],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["copies"], 1)
        self.assertTrue(out["prompt"].strip())
        self.assertEqual(out["selected"], out["prompt"])
        self.assertIn("press Ctrl/⌘C", out["status"])
        self.assertTrue(out["focusedHeading"])
        self.assertEqual(out["activeStep"], "step-1")


if __name__ == "__main__":
    unittest.main()

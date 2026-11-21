#!/usr/bin/env python3
"""The standalone overview is a safe, self-contained companion to the owner tour."""
from pathlib import Path
import json
import re
import shutil
import sys
import subprocess
import tempfile
import unittest

PAGE = Path(__file__).resolve().parent.parent / "skills" / "tutorial" / "assets" / "getting-started.html"


sys.path.insert(0, str(PAGE.parent.parent / "scripts"))
import tour_page


class OverviewTest(unittest.TestCase):
    def test_four_steps_and_demo_boundary(self):
        # Emitted copy and demo boundaries are intentional data pins.
        page = PAGE.read_text()
        self.assertEqual(re.findall(r'id="step-(\d)"', page), ["0", "1", "2", "3"])
        self.assertIn("Explore example connections, permissions and workflows", page)
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
                         "Room-call screen sharing is work in progress",
                         "confirm the schedule was created", "setCommons('board'"):
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
        self.check_behaviour(PAGE)

    def test_published_page_opens_overview_and_preserves_checklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp) / "tutorial.html"
            page = tour_page.render_page("Test Agent", agent="@test:example.test")
            self.assertTrue(tour_page.page_digest_ok(page))
            rendered.write_text(page)
            self.check_behaviour(rendered, embedded=True)

    def check_behaviour(self, page, embedded=False):
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
  result.providers = [...d.querySelectorAll('#step-0 .sub-tab')].map(button => {
    button.click();
    return { label: button.textContent, text: d.getElementById('providerInfo').textContent,
      active: button.classList.contains('active') };
  });
  result.topics = [...d.querySelectorAll('.tab-btn')].map(button => {
    button.click();
    return { label: button.textContent.trim(), status: d.getElementById('progressStatus').textContent,
      focused: d.activeElement === d.querySelector('.tour-card.active h2') };
  });
  result.tiers = [...d.querySelectorAll('#step-1 .sub-tab')].map(button => {
    button.click(); return d.getElementById('tierInfo').textContent;
  });
  result.scenarios = [...d.querySelectorAll('#step-2 .sub-tab')].map(button => {
    button.click(); return { label: button.textContent, text: d.getElementById('callInfo').textContent };
  });
  const checklist = d.querySelector('#tutorial-checklist');
  if (checklist) {
    const summary = checklist.querySelector('summary');
    result.startsClosed = !checklist.open;
    result.label = summary.textContent;
    result.overviewFirst = Boolean(d.querySelector('#overview').compareDocumentPosition(checklist) & 4);
    summary.click();
    result.opens = checklist.open;
    result.stepCount = checklist.querySelectorAll('section.sec').length;
    result.personalized = checklist.querySelector('h1').textContent;
    const first = checklist.querySelector('input[data-done]');
    for (let i = 0; first.disabled && i < 100; i++) {
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    first.click();
    result.done = first.checked;
    result.saved = JSON.parse(dom.window.localStorage.getItem('gs-done'));
    summary.click();
    summary.click();
    result.doneAfterReopen = first.checked;
  }
  result.errors = errors;
  console.log(JSON.stringify(result));
  dom.window.close();
})();
"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "overview.cjs"
            target.write_text(harness)
            result = subprocess.run([node, str(target), str(page)],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual([p["label"] for p in out["providers"]], [
            "Antigravity", "Claude Code", "Claude Duet", "Codex", "Dot",
            "Grok Bot", "Muse", "Sutando (Local Mac)", "Other apps"])
        for provider in out["providers"]:
            self.assertTrue(provider["active"])
            self.assertTrue(provider["text"].strip())
        providers = {p["label"]: p["text"] for p in out["providers"]}
        self.assertIn("host supports it", providers["Antigravity"])
        self.assertIn("separate from Claude Duet", providers["Claude Code"])
        self.assertIn("separate from Dot", providers["Codex"])
        self.assertIn("Hermes and OpenClaw", providers["Other apps"])
        self.assertEqual([t["status"] for t in out["topics"]],
                         ["Ecosystem", "Multiplayer", "Calls & Local Tools", "Commons & Tasks"])
        self.assertTrue(all(t["focused"] for t in out["topics"]))
        self.assertIn("owner can still access and invoke", out["tiers"][2])
        self.assertIn("other room members cannot", out["tiers"][2])
        scenarios = {s["label"]: s["text"] for s in out["scenarios"]}
        self.assertIn("supported, configured local session", scenarios["Live screen coach"])
        self.assertIn("approved local screen access", scenarios["Live screen coach"])
        self.assertIn("authorize the edit", scenarios["Write & edit by voice"])
        self.assertIn("paste the passage", scenarios["Write & edit by voice"])
        if embedded:
            self.assertTrue(out["overviewFirst"])
            self.assertTrue(out["startsClosed"])
            self.assertEqual(out["label"], "Try it step by step")
            self.assertTrue(out["opens"])
            self.assertEqual(out["stepCount"], 10)
            self.assertIn("Test Agent", out["personalized"])
            self.assertTrue(out["done"])
            self.assertEqual(out["saved"], ["dm"])
            self.assertTrue(out["doneAfterReopen"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["copies"], 1)
        self.assertTrue(out["prompt"].strip())
        self.assertEqual(out["selected"], out["prompt"])
        self.assertIn("press Ctrl/⌘C", out["status"])
        self.assertTrue(out["focusedHeading"])
        self.assertEqual(out["activeStep"], "step-1")


if __name__ == "__main__":
    unittest.main()

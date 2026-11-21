#!/usr/bin/env python3
"""Offline fixtures for the owner tutorial and the Commons draft handoff."""
import base64
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "tutorial" / "scripts"
sys.path.insert(0, str(SCRIPT))
import tour_page as tour


class NativeGuideTest(unittest.TestCase):
    def setUp(self):
        self.page = tour.render_page("Example & Co", roles="Designer",
                                     agent="@example.agent:example.test", private_setup=True,
                                     prompts={"dm": "Plan my next review & reserve discussion time."})

    def test_verified_private_setup_and_safe_fallbacks(self):
        self.assertIn("<span>Example &amp; Co</span>", self.page)
        self.assertIn("Picked for: Designer", self.page)
        self.assertIn("Plan my next review &amp; reserve discussion time.", self.page)
        self.assertIn('id="guide-draft"', self.page)
        self.assertTrue(tour.page_digest_ok(self.page))
        self.assertNotIn("navigator.clipboard", self.page)
        self.assertNotIn("Paste in:", self.page)
        self.assertNotRegex(self.page, r"\{\{[A-Z_]+\}\}")
        for page in (
            tour.render_page("Example & Co", roles="Designer", native_dm=True,
                             prompts={"dm": "Private example."}),
            tour.render_page("Example & Co", roles="Designer", audience="room", private_setup=True,
                             prompts={"dm": "Private example."}),
        ):
            self.assertNotIn("Example &amp; Co", page)
            self.assertNotIn("Designer", page)
            self.assertNotIn("Private example.", page)

    def test_keeps_one_mobile_step_and_original_app_store_qr(self):
        self.assertEqual(re.findall(r'type="checkbox" value="([a-z]+)"', self.page),
                         ["dm", "rooms", "commons", "agents", "teamwork", "calls", "apps", "activity", "mobile"])
        self.assertIn('href="https://apps.apple.com/us/app/s-space-human-ai-workspace/id6788541784"', self.page)
        qr = base64.b64decode(re.search(r'data:image/png;base64,([A-Za-z0-9+/=]+)', self.page)[1])
        self.assertEqual(hashlib.sha256(qr).hexdigest(),
                         "0c1c20a57249750a6795c7e29843b1f9ccf6c54d68a3b33f398444552ca1f030")

    def test_edit_select_confirm_and_no_bridge(self):
        node = shutil.which("node")
        if not node or subprocess.run([node, "-e", "require('jsdom')"], capture_output=True).returncode:
            self.skipTest("Node with jsdom unavailable")
        with tempfile.TemporaryDirectory() as work:
            page = Path(work) / "guide.html"
            page.write_text(self.page, encoding="utf-8")
            js = Path(__file__).parent / "fixtures" / "tutorial-native-drafts.cjs"
            run = subprocess.run([node, str(js), str(page)], capture_output=True, text=True, timeout=25)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()

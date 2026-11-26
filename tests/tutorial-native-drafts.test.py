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
from unittest import mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "skills" / "tutorial" / "scripts"
sys.path.insert(0, str(SCRIPT))
import getting_started as gs
import tour_page as tour


class NativeGuideTest(unittest.TestCase):
    def setUp(self):
        self.page = tour.render_page("Example & Co", roles="Designer",
                                     agent="@example.agent:example.test", private_setup=True,
                                     prompts={"dm": "Plan my next review & reserve discussion time."})

    def test_fictional_local_preview_and_safe_fallbacks(self):
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

    def test_local_preview_strips_invisible_controls_but_keeps_visible_unicode(self):
        page = tour.render_page("Zoë\u202e\u200b 大\u2066地 & Co", private_setup=True)
        self.assertIn("<span>Zoë 大地 &amp; Co</span>", page)
        for invisible in ("\u202e", "\u200b", "\u2066"):
            self.assertNotIn(invisible, page)

    def test_pre_fix_journals_cannot_replay_or_summon(self):
        with tempfile.TemporaryDirectory() as work:
            marker = gs.marker_path(Path(work))
            room = "!example:example.test"
            commons = mock.Mock(spec=gs.Commons, room=room)
            for state in ("creating", "pending", "updating"):
                with self.subTest(state=state):
                    old = {"state": state, "kind": "html-example", "operation_id": "example-op",
                           "target": self.page, "before": "", "hash": gs.content_hash(self.page)}
                    gs.save_marker(marker, {room: old})
                    calls = ((gs.recover(commons, marker), "not replayed"),
                             (gs.publish(commons, marker, "Example Agent"), "ownership not verified"))
                    for result, reason in calls:
                        self.assertEqual((result["action"], result["summoned"]), ("skipped", False))
                        self.assertIn(reason, result["reason"])
                    self.assertEqual(commons.mock_calls, [])
                    self.assertEqual(gs.load_marker(marker)[room], old)

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

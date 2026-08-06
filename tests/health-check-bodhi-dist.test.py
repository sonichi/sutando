#!/usr/bin/env python3
"""Regression test: check_bodhi_dist must scan whichever bodhi artifact the
voice-agent ACTUALLY loads, which differs by deployment.

  dev checkout  -> node_modules/bodhi-realtime-agent/dist/index.js
  bundled app   -> dist/voice-agent.js (esbuild bundle, bodhi inlined,
                   NO node_modules on disk at all)

The original implementation only checked the node_modules path. On a bundled
install that path cannot exist, so the probe warned "run `npm install`" on
every tick — noise that reads as benign — while giving ZERO coverage of the
bundle actually running. Per check_bodhi_dist's own docstring it is the only
probe for a stale bodhi (voice-agent boots fine and fails at 1007 only once a
client connects), so the regression it exists to catch would have shipped
undetected in exactly the deployment where it matters.

The two cases that matter most here are `bundled_ok` (proves the bundled
layout is scanned at all) and `stale_*` (proves the probe still FAILS on the
regression — a check that passes everywhere is worthless).

Run: python3 tests/health-check-bodhi-dist.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Minimal stand-ins for the two transports. The Gemini one is identified by
# its sendRealtimeInput call; `media:` is the deprecated pre-fix key that
# Gemini 3.1 rejects with a 1007.
GEMINI_OK = """
class GeminiTransport {
  sendAudio(base64Data) {
    if (!this.session) return;
    this.session.sendRealtimeInput({ audio: { data: base64Data, mimeType: "audio/pcm;rate=16000" } });
  }
  sendFile(base64Data, mimeType) {
    if (!this.session) return;
    this.session.sendRealtimeInput({ video: { data: base64Data, mimeType } });
  }
}
"""

GEMINI_STALE_AUDIO = GEMINI_OK.replace(
    "sendRealtimeInput({ audio: { data", "sendRealtimeInput({ media: { data"
)
GEMINI_STALE_FILE = GEMINI_OK.replace(
    "sendRealtimeInput({ video: { data", "sendRealtimeInput({ media: { data"
)


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_bodhi_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestBodhiDistArtifactSelection(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.REPO_DIR = self.root

    def tearDown(self):
        self._tmp.cleanup()

    def _write_bundle(self, text: str) -> Path:
        d = self.root / "dist"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "voice-agent.js"
        p.write_text(text)
        return p

    def _write_node_modules(self, text: str) -> Path:
        d = self.root / "node_modules" / "bodhi-realtime-agent" / "dist"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "index.js"
        p.write_text(text)
        return p

    def test_bundled_install_is_scanned(self):
        """The regression: a bundled install has no node_modules, and the
        bundle must be scanned rather than reported as 'not found'."""
        self._write_bundle(GEMINI_OK)
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("voice-agent.js", r["detail"])

    def test_dev_layout_preferred_when_present(self):
        """A dev checkout must report on the package it actually resolves."""
        self._write_node_modules(GEMINI_OK)
        self._write_bundle(GEMINI_OK)
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "ok", r["detail"])
        self.assertIn("index.js", r["detail"])

    def test_neither_artifact_present_warns(self):
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "warn")
        self.assertIn("no bodhi artifact found", r["detail"])

    def test_stale_audio_in_bundle_still_fails(self):
        """A check that passes everywhere is worthless — the whole point of
        this probe is catching the deprecated `media` key."""
        self._write_bundle(GEMINI_STALE_AUDIO)
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "fail", r["detail"])
        self.assertIn("sendAudio", r["detail"])

    def test_stale_file_in_bundle_still_fails(self):
        self._write_bundle(GEMINI_STALE_FILE)
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "fail", r["detail"])
        self.assertIn("sendFile", r["detail"])

    def test_unrecognizable_artifact_names_the_file(self):
        """No sendAudio at all (e.g. a truncated or wrong bundle) must warn
        and say WHICH file was scanned, so the two layouts stay
        distinguishable in the health output."""
        self._write_bundle("export const nothingUseful = 1;\n")
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "warn")
        self.assertIn("could not locate sendAudio", r["detail"])
        self.assertIn("voice-agent.js", r["detail"])

    def test_unreadable_artifact_warns(self):
        """A directory where the file is expected makes read_text raise
        OSError; the probe must degrade to a warn, not crash the tick."""
        d = self.root / "dist" / "voice-agent.js"
        d.mkdir(parents=True, exist_ok=True)
        r = self.hc.check_bodhi_dist()
        self.assertEqual(r["status"], "warn")
        self.assertIn("dist read failed", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

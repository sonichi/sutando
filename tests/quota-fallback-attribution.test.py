#!/usr/bin/env python3
"""A fallback rewrite must not change WHO a quota record or a rejection belongs to.

The credential proxy rewrites an Opus request to Sonnet when a window is over its
line. The quota consumers identify a seat by the model it REQUESTS (its own
ANTHROPIC_MODEL), so the proxy must stamp that model and carry the rewritten one
separately. Stamping the rewritten model broke two consumers (PR #5275 review):

  - `record_speaks_for_seat` said a record refreshed by the Opus seat's own
    traffic was "another model's", so a held seat could never clear its limit
    banner -- and the probe that should refresh it is rewritten too;
  - `check_core_request_rejections` reported the Opus seat's rejections as
    "none for this core's model".

It also covers the same-tier handoff (review point 3): 7d resets while 5h takes
tier 3 over. The proxy kept rewriting but saved nothing, so the record still said
the EXPIRED 7d window held the tier and `check_quota_model_fallback` reported it
"cleared".

This test crosses the language boundary on purpose: it runs the REAL proxy with
its REAL writers (tests/fixtures/fallback-attribution-driver.ts) and reads the
file it wrote with the REAL Python consumers, so a drift on either side fails.

Run: python3 tests/quota-fallback-attribution.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import quota_availability as qa  # noqa: E402

DRIVER = REPO / "tests" / "fixtures" / "fallback-attribution-driver.ts"
TSX = REPO / "node_modules" / ".bin" / "tsx"
REQUESTED = "claude-opus-5-5"


def _load_health_check():
    spec = importlib.util.spec_from_file_location("hc_attr_test", REPO / "src" / "health-check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _drive(out: Path, mode: str = "5") -> dict:
    """Run the real proxy through one scenario; return what the upstream received.
    `mode` is a rejection count, or `handoff`."""
    run = subprocess.run([str(TSX), str(DRIVER), str(out), mode],
                         cwd=REPO, capture_output=True, text=True, timeout=120)
    if run.returncode != 0:
        raise AssertionError(f"driver failed ({run.returncode}):\n{run.stderr[-2000:]}")
    return json.loads(run.stdout.strip().splitlines()[-1])


_HAVE_TSX = TSX.exists() and bool(shutil.which("node"))


# Locally a missing tsx skips. In CI it FAILS: the python job runs `npm ci`, so a
# skip there would mean this cross-language guard silently stopped running.
@unittest.skipUnless(_HAVE_TSX or os.environ.get("CI"), "needs node_modules/.bin/tsx (run npm ci)")
class TestFallbackAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _HAVE_TSX:
            raise AssertionError("CI without node_modules/.bin/tsx: this guard would not run")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "quota-state.json"
        cls.sent = _drive(cls.out)
        cls.state = json.loads(cls.out.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_request_really_was_rewritten(self):
        # Control: without a rewrite the rest of this file proves nothing.
        self.assertEqual(self.sent["requested"], REQUESTED)
        self.assertTrue(any(m != REQUESTED for m in self.sent["sent"][1:]),
                        f"expected a rewrite past the first response, upstream saw {self.sent['sent']}")

    def test_the_quota_record_belongs_to_the_requested_model(self):
        last = self.state["last_request"]
        self.assertEqual(last["model"], REQUESTED, "the seat's identity is the model it asked for")
        self.assertTrue(last.get("effective_model"), "the model that actually ran is carried too")
        self.assertNotEqual(last["effective_model"], REQUESTED)
        self.assertEqual(qa.record_model(self.state), REQUESTED)

    def test_a_held_opus_seat_is_vouched_for_by_its_own_rewritten_traffic(self):
        seat = qa.SeatEnv(observed=True, base_url="http://localhost:7846", model=REQUESTED)
        self.assertTrue(qa.record_speaks_for_seat(self.state, seat, self._tmp.name),
                        "a record refreshed by this seat's own traffic must speak for this seat")

    def test_a_different_seat_is_still_not_vouched_for(self):
        # The fix must not widen the gate: a Sonnet seat is not the Opus seat,
        # even though the Opus seat's requests ran on Sonnet.
        seat = qa.SeatEnv(observed=True, base_url="http://localhost:7846", model="claude-haiku-4-5")
        self.assertFalse(qa.record_speaks_for_seat(self.state, seat, self._tmp.name))

    def test_rejections_are_counted_against_the_requested_model(self):
        ledger = self.state["recent_rejections"]
        self.assertEqual(len(ledger), 5)
        for entry in ledger:
            self.assertEqual(entry["model"], REQUESTED)
            self.assertTrue(entry.get("effective_model"))
            self.assertNotEqual(entry["effective_model"], REQUESTED)

        hc = _load_health_check()
        root = Path(self._tmp.name) / "ws"
        hc.WORKSPACE_DIR = root
        target = hc.status_read_path("quota-state.json", root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.out.read_text())
        with unittest.mock.patch.dict(os.environ, {"SUTANDO_CORE_MODEL": REQUESTED}), \
                unittest.mock.patch.object(hc, "_settings_model_pins", lambda *a, **k: []):
            check = hc.check_core_request_rejections()
        self.assertEqual(check["status"], "fail", check.get("detail"))
        self.assertNotIn("none for this core's model", check["detail"])
        effective = ledger[-1]["effective_model"]
        self.assertIn(f"served as {effective}", check["detail"],
                      "the owner is told the rejected request actually ran on the fallback model")


@unittest.skipUnless(_HAVE_TSX or os.environ.get("CI"), "needs node_modules/.bin/tsx (run npm ci)")
class TestSameTierHandoff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _HAVE_TSX:
            raise AssertionError("CI without node_modules/.bin/tsx: this guard would not run")
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name) / "quota-state.json"
        cls.sent = _drive(cls.out, "handoff")
        cls.state = json.loads(cls.out.read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_proxy_kept_rewriting_through_the_handoff(self):
        # Control: the tier really held, so "cleared" would be a false report.
        self.assertTrue(all(m != REQUESTED for m in self.sent["sent"][1:]), self.sent["sent"])

    def test_the_saved_state_names_the_window_that_holds_the_tier_now(self):
        fb = self.state["fallback"]
        self.assertEqual(fb["tier"], 3)
        self.assertEqual(fb["windows"]["5h"]["tier"], 3)
        self.assertEqual(fb["windows"]["7d"]["tier"], 1)
        self.assertEqual(fb["fired"]["window"], "5h")
        self.assertIn("5h", fb["reason"])

    def test_the_health_check_does_not_call_a_live_fallback_cleared(self):
        hc = _load_health_check()
        root = Path(self._tmp.name) / "ws"
        hc.WORKSPACE_DIR = root
        target = hc.status_read_path("quota-state.json", root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.out.read_text())
        check = hc.check_quota_model_fallback()
        self.assertEqual(check["status"], "warn", check.get("detail"))
        self.assertNotIn("cleared", check["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

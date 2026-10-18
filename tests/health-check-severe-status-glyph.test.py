#!/usr/bin/env python3
"""A `fail` must not render with a less alarming glyph than a `down`.

Regression: the human-readable listing enumerated `down`/`missing`/`not_loaded` as
severe and fell back to "~" for everything else — which put `fail`, the most severe
status any probe emits (9 probes use it), on the catch-all glyph, sharing it with
"status I don't recognize". `error` (5 probes) and `wedged` landed there too.

Found the hard way on a peer host: their health-check filter was `grep -E "⚠|✗"`, so
the one `fail` line was exactly the line the filter hid, and a run with a real failure
read as three routine warnings.

`--quiet` never had this bug — it renders every non-stale issue as `✗`. The two output
modes disagreed about the same status, which is the tell that the mapping belonged in
one place.

Run: python3 tests/health-check-severe-status-glyph.test.py
"""
from __future__ import annotations
import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "health-check.py"

# The same predicate the issue list uses: issues = status not in ("ok", "warn").
BENIGN = ("ok", "warn")
# `stale` is an issue, but it earns a distinct glyph because it names a remedy.
DISTINCT = ("stale",)


def _load():
    spec = importlib.util.spec_from_file_location("hc_icon_test", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestSevereStatusGlyph(unittest.TestCase):
    def setUp(self):
        self.hc = _load()

    def test_fail_is_not_softer_than_down(self):
        # The specific regression. A reader scanning for trouble must see the same
        # mark for the worst status as for a dead service.
        self.assertEqual(self.hc.status_icon("fail"), self.hc.status_icon("down"))
        self.assertEqual(self.hc.status_icon("fail"), "✗")

    def test_EVERY_status_the_probes_actually_emit_is_classified(self):
        # Derived by scanning the source rather than hand-listing: a status added
        # next month is covered by this test the day it appears, which is the only
        # way the assertion stays true after I stop looking at it.
        emitted = set(re.findall(r'["\']status["\']\s*:\s*["\'](\w+)["\']', SRC.read_text()))
        self.assertGreaterEqual(len(emitted), 8, f"scan looks broken, found {emitted}")
        for st in ("fail", "error", "wedged"):
            self.assertIn(st, emitted, "fixture drift: this status no longer exists in the source")
        for st in sorted(emitted):
            with self.subTest(status=st):
                icon = self.hc.status_icon(st)
                self.assertNotEqual(icon, "~", f"{st!r} renders as the catch-all")
                if st not in BENIGN and st not in DISTINCT:
                    self.assertEqual(icon, "✗", f"issue status {st!r} must read as a problem")

    def test_an_UNKNOWN_status_reads_as_a_problem_not_a_shrug(self):
        # The inverted default. Whoever adds a status next gets a loud glyph until
        # they deliberately classify it — the failure mode here was a quiet one.
        self.assertEqual(self.hc.status_icon("some_status_added_next_month"), "✗")

    def test_benign_and_distinct_statuses_keep_their_glyphs(self):
        # Over-trigger control: this must not turn a healthy run into a wall of ✗.
        self.assertEqual(self.hc.status_icon("ok"), "✓")
        self.assertEqual(self.hc.status_icon("warn"), "⚠")
        self.assertEqual(self.hc.status_icon("stale"), "♻")

    def test_both_output_modes_derive_from_the_same_mapping(self):
        # The bug existed because --quiet and human mode each spelled the mapping
        # out. Unit-testing the helper proves nothing if a call site still inlines
        # its own chain, so pin that neither does.
        src = SRC.read_text()
        inline = re.findall(r'icon\s*=\s*(?!status_icon)\S.*', src)
        self.assertEqual(inline, [], f"a render site still inlines its own icon chain: {inline}")
        self.assertEqual(src.count("icon = status_icon(c[\"status\"])"), 2,
                         "expected both render sites to go through status_icon()")
        self.assertNotIn('else "~"', src, "the catch-all glyph is still reachable")


if __name__ == "__main__":
    unittest.main(verbosity=2)

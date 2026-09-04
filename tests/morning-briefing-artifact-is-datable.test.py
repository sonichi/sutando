#!/usr/bin/env python3
"""The briefing's result filename must be measurable by the punctuality probe.

`_daily_artifact_minutes` matches `<stem>-YYYY-MM-DD`; an epoch suffix never
does, so the daily deliverable was unverifiable while running fine.
"""
import importlib.util
import re
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load("health_check", ROOT / "src" / "health-check.py")
SRC = (ROOT / "src" / "morning-briefing.py").read_text()


class BriefingArtifactIsDatable(unittest.TestCase):
    def test_the_written_name_carries_a_DATE_the_probe_can_match(self):
        m = re.search(r'RESULTS_DIR / f"(proactive-morning-[^"]+)"', SRC)
        self.assertIsNotNone(m, "could not find the result filename in morning-briefing.py")
        rendered = m.group(1).replace(
            "{time.strftime('%Y-%m-%d')}", time.strftime("%Y-%m-%d")).replace("{ts}", "1788000000000")
        self.assertRegex(rendered, r"^proactive-morning-\d{4}-\d{2}-\d{2}-",
                         f"the probe matches <stem>-YYYY-MM-DD; this name is {rendered!r}")

    def test_the_probe_actually_finds_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            (results / f"proactive-morning-{time.strftime('%Y-%m-%d')}-1788000000000.txt").write_text("x")
            found = hc._daily_artifact_minutes(results, "proactive-morning")
            self.assertEqual([d for d, _ in found], [time.strftime("%Y-%m-%d")],
                             "the punctuality probe cannot see the briefing's own output")

    def test_an_epoch_only_name_is_invisible_to_the_probe(self):
        # The control: this is what shipped from 2026-07-16, and it is why the
        # probe reported UNCHECKED for a job that was running fine.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            results.mkdir()
            (results / "proactive-morning-1788000000000.txt").write_text("x")
            self.assertEqual(hc._daily_artifact_minutes(results, "proactive-morning"), [])

    def test_the_delivery_prefix_is_unchanged(self):
        # Every bridge drains on the `proactive-` prefix; the date must not
        # move it out of that namespace.
        self.assertTrue(f"proactive-morning-{time.strftime('%Y-%m-%d')}-1.txt".startswith("proactive-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

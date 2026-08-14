#!/usr/bin/env python3
"""An append-only log that shrinks is a truncation, and size alone cannot see it.

`check_file` answers "is it empty". On 2026-08-14 this repo's `build_log.md` went
from ~314 KB to 1673 bytes — one surviving entry, the rest destroyed by an
`open(p,"w").write(open(p).read()+…)` append — and `health-check` reported
`✓ build_log.md ok 1673 bytes` for thirteen hours. The 2026-08-09 instance of the
same bug WAS caught, because that one left the file empty. A truncation that
leaves anything is invisible to an emptiness check.

`test_a_large_shrink_warns` fails on the parent commit, where `build_log.md` is
routed through `check_file` and any non-zero size reads `ok`.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_shrink", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hc_shrink"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load()


class AppendOnlyShrinkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        ws = mock.patch.object(hc, "WORKSPACE_DIR", self.ws)
        ws.start(); self.addCleanup(ws.stop)
        self.log = self.ws / "build_log.md"

    def _write(self, n):
        self.log.write_text("x" * n)

    def _check(self):
        # Fall back to the PARENT's routing when the probe is absent, so the
        # control arm FAILS these assertions ("ok" where "warn" is required)
        # instead of erroring in every case. An AttributeError proves the symbol
        # is new; it does not show that the behaviour differs.
        fn = getattr(hc, "check_append_only_file", None)
        if fn is None:
            return hc.check_file(self.log, "build_log.md")
        return fn(self.log, "build_log.md")

    def _mark(self):
        f = self.ws / "state" / "file-watermarks.json"
        return json.loads(f.read_text())["build_log.md"] if f.is_file() else None

    # ---- THE regression pin: fails on the parent -----------------------------

    def test_a_large_shrink_warns(self):
        """314 KB -> 1673 bytes is the real incident, scaled down."""
        self._write(314_000); self._check()
        self._write(1_673)
        out = self._check()
        self.assertEqual(out["status"], "warn", out)
        self.assertIn("1673", out["detail"])
        self.assertIn("314000", out["detail"])

    def test_the_warn_names_the_drop_and_says_it_re_baselines(self):
        self._write(100_000); self._check()
        self._write(1_000)
        detail = self._check()["detail"]
        self.assertIn("99% drop", detail)
        self.assertIn("warns once", detail)

    def test_it_warns_once_then_re_baselines(self):
        """A permanent warn is one nobody reads. One loud signal, then settle —
        and a SECOND truncation later must still be caught."""
        self._write(100_000); self._check()
        self._write(1_000)
        self.assertEqual(self._check()["status"], "warn")
        self.assertEqual(self._check()["status"], "ok", "should re-baseline after warning")
        self.assertEqual(self._mark(), 1_000)
        self._write(10)
        self.assertEqual(self._check()["status"], "warn", "a later truncation must still warn")

    # ---- must NOT fire ------------------------------------------------------

    def test_growth_is_ok_and_raises_the_mark(self):
        self._write(1_000); self._check()
        self._write(5_000)
        self.assertEqual(self._check()["status"], "ok")
        self.assertEqual(self._mark(), 5_000)

    def test_a_small_shrink_does_not_warn_and_does_not_lower_the_mark(self):
        """Trimming a few lines is not a truncation. The mark must survive it,
        or a slow series of small edits would silently reset the baseline."""
        self._write(1_000); self._check()
        self._write(900)
        self.assertEqual(self._check()["status"], "ok")
        self.assertEqual(self._mark(), 1_000)

    def test_first_sight_records_without_warning(self):
        self._write(500)
        self.assertEqual(self._check()["status"], "ok")
        self.assertEqual(self._mark(), 500)

    # ---- degraded paths -----------------------------------------------------

    def test_missing_and_empty_delegate_to_check_file(self):
        self.assertEqual(self._check()["status"], "missing")
        self._write(0)
        self.assertEqual(self._check()["status"], "empty")

    def test_an_unreadable_watermark_file_is_not_evidence_of_a_shrink(self):
        """Corrupt state must degrade to the plain answer, never invent a warn."""
        self._write(100_000); self._check()
        (self.ws / "state" / "file-watermarks.json").write_text("{not json")
        self._write(10)
        self.assertEqual(self._check()["status"], "ok")

    def test_a_non_dict_watermark_file_is_survivable(self):
        self._write(100_000)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        (self.ws / "state" / "file-watermarks.json").write_text('["not", "a", "dict"]')
        self.assertEqual(self._check()["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Census contract: counts every verdict class, names unsigned writers by
source, honors the day window against the immutable id timestamp (not
mtime), and never mints a key (verification-only on keyless hosts)."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import task_envelope as E  # noqa: E402
import task_envelope_census as C  # noqa: E402


def _write(ws: Path, name: str, body: str, sub: str = "tasks") -> Path:
    d = ws / sub
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


class CensusContract(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="census-ws-")
        self.ws = Path(self._tmp.name)
        (self.ws / "state" / "auth").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _now_id(self, suffix: str) -> str:
        return f"task-{int(time.time()*1000)}{suffix}.txt"

    def test_counts_and_unsigned_sources(self):
        signed = E.stamp_text(
            "id: task-1\ntask: t\nsource: ag2space\naccess_tier: owner\n",
            self.ws)
        _write(self.ws, self._now_id("1"), signed)
        _write(self.ws, self._now_id("2"),
               "id: task-2\ntask: t\nsource: discord\n")
        tampered = signed.replace("task: t", "task: EVIL")
        _write(self.ws, self._now_id("3"), tampered, sub="tasks/archive")
        r = C.census(self.ws)
        self.assertEqual(r["scanned"], 3)
        self.assertEqual(r["verdicts"].get("verified"), 1)
        self.assertEqual(r["verdicts"].get("unsigned"), 1)
        self.assertEqual(r["verdicts"].get("invalid"), 1)
        self.assertEqual(r["unsigned_sources"], ["discord"])

    def test_window_uses_id_epoch_not_mtime(self):
        old_ms = int((time.time() - 30 * 86400) * 1000)
        _write(self.ws, f"task-{old_ms}.txt",
               "id: task-old\ntask: t\nsource: voice\n")
        r = C.census(self.ws, days=7)
        self.assertEqual(r["scanned"], 0,
                         "fresh mtime must not resurrect an old task id")

    def test_monthly_archive_subdirs_are_counted(self):
        """Review blocker: task_archive.py nests tasks/archive/YYYY-MM/ —
        a writer whose tasks were archived there must still be named."""
        _write(self.ws, self._now_id("a"),
               "id: task-m\ntask: t\nsource: voice\n",
               sub="tasks/archive/2026-08")
        r = C.census(self.ws)
        self.assertEqual(r["scanned"], 1)
        self.assertEqual(r["unsigned_sources"], ["voice"])

    def test_keyless_host_reports_unverifiable_and_mints_nothing(self):
        signed = E.stamp_text("id: task-9\ntask: t\nsource: x\n", self.ws)
        key = E.key_path(self.ws)
        key.unlink()
        _write(self.ws, self._now_id("9"), signed)
        r = C.census(self.ws)
        self.assertEqual(r["verdicts"].get("unverifiable"), 1)
        self.assertFalse(key.exists(), "census must never create the key")


class CensusCliAndFallbacks(unittest.TestCase):
    """In-process CLI + parser-fallback coverage (subprocess runs are
    invisible to the coverage gate — same lesson as the envelope CLI)."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="census-cli-")
        self.ws = Path(self._tmp.name)
        (self.ws / "state" / "auth").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_timestamp_header_fallback_for_hex_ids(self):
        old_iso = "2026-01-01T00:00:00Z"
        _write(self.ws, "task-deadbeefcafe.txt",
               f"id: task-deadbeefcafe\ntimestamp: {old_iso}\ntask: t\n"
               "source: slack\n")
        r = C.census(self.ws, days=7)
        self.assertEqual(r["scanned"], 0,
                         "hex id must fall back to the timestamp header, "
                         "which is far outside the window")
        r = C.census(self.ws, days=10 * 365)
        self.assertEqual(r["scanned"], 1)

    def test_long_digit_id_is_not_an_epoch(self):
        """Live bug (2026-08-17 census run): an 18-digit gateway id's first 13
        digits parse as a 2057 epoch, pinning July archive files in-window."""
        old_iso = "2026-01-01T00:00:00Z"
        _write(self.ws, "task-274606259064310678.txt",
               f"id: task-274606259064310678\ntimestamp: {old_iso}\n"
               "task: t\nsource: ag2space\n", sub="tasks/archive/2026-01")
        r = C.census(self.ws, days=7)
        self.assertEqual(r["scanned"], 0,
                         "an over-long digit id must fall back to the "
                         "timestamp header, not parse a future epoch")

    def test_bad_timestamp_header_falls_back_to_mtime(self):
        _write(self.ws, "task-cafebabe.txt",
               "id: task-cafebabe\ntimestamp: not-a-date\ntask: t\n")
        r = C.census(self.ws, days=7)
        self.assertEqual(r["scanned"], 1, "fresh mtime keeps it in-window")
        self.assertEqual(r["unsigned_sources"], ["(none)"])

    def test_unreadable_file_is_skipped(self):
        p = _write(self.ws, f"task-{int(time.time()*1000)}u.txt",
                   "id: task-u\ntask: t\nsource: x\n")
        real_read = Path.read_text

        def deny(self_p, *a, **kw):
            if self_p == p:
                raise OSError("denied")
            return real_read(self_p, *a, **kw)
        Path.read_text = deny
        try:
            r = C.census(self.ws)
        finally:
            Path.read_text = real_read
        self.assertEqual(r["scanned"], 0)

    def test_main_text_and_json_paths(self):
        import contextlib
        import io
        import json as _json
        signed = E.stamp_text(
            "id: task-1\ntask: t\nsource: gateway\naccess_tier: owner\n",
            self.ws)
        _write(self.ws, self._now_id("m"), signed)
        _write(self.ws, self._now_id("n"),
               "id: task-2\ntask: t\nsource: voice\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = C.main(["x", "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("UNSIGNED writers still live", text)
        self.assertIn("voice", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = C.main(["x", "--json", "--workspace", str(self.ws)])
        self.assertEqual(rc, 0)
        j = _json.loads(out.getvalue())
        self.assertEqual(j["verdicts"]["verified"], 1)
        self.assertEqual(j["unsigned_sources"], ["voice"])

    def test_file_vanishing_between_read_and_stat_is_skipped(self):
        # Archiver interleaving: the file is readable, then moved before
        # stat(). The census must skip the claimed file, not abort.
        from unittest import mock
        signed = E.stamp_text("id: task-s\ntask: t\nsource: gateway\n",
                              self.ws)
        _write(self.ws, self._now_id("s"), signed)
        victim = _write(self.ws, self._now_id("gone"),
                        "id: task-g\ntask: t\nsource: voice\n")
        real_read = Path.read_text

        def read_then_archive(p, *a, **kw):
            text = real_read(p, *a, **kw)
            if p.name == victim.name:
                p.unlink()
            return text

        with mock.patch.object(Path, "read_text", read_then_archive):
            result = C.census(self.ws)
        self.assertEqual(result["scanned"], 1)
        self.assertNotIn("voice", result["by_source"])

    def test_main_gate_met_message_when_all_verified(self):
        import contextlib
        import io
        signed = E.stamp_text("id: task-3\ntask: t\nsource: gateway\n",
                              self.ws)
        _write(self.ws, self._now_id("v"), signed)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(C.main(["x", "--workspace", str(self.ws)]), 0)
        self.assertIn("census gate MET", out.getvalue())

    def _now_id(self, suffix: str) -> str:
        return f"task-{int(time.time()*1000)}{suffix}.txt"


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

    def test_keyless_host_reports_unverifiable_and_mints_nothing(self):
        signed = E.stamp_text("id: task-9\ntask: t\nsource: x\n", self.ws)
        key = E.key_path(self.ws)
        key.unlink()
        _write(self.ws, self._now_id("9"), signed)
        r = C.census(self.ws)
        self.assertEqual(r["verdicts"].get("unverifiable"), 1)
        self.assertFalse(key.exists(), "census must never create the key")


if __name__ == "__main__":
    unittest.main(verbosity=2)

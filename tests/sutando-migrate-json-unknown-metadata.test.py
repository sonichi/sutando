#!/usr/bin/env python3
"""--json must never certify unknown metadata as identical content.

The scan's Python aggregator maps an unreadable mtime/size to None, and
`len({(mtime, size)}) == 1` then counted two misses as a witnessed match: a
collision whose bytes differ was published as `identical_content` with zero
`genuine_conflicts` — a machine-readable plan calling a divergent collision a
safe duplicate. The human reporter already kept unknown rows distinct; this
pins the JSON path to the same rule, for BOTH unavailable fields.

Failure is injected at the real seam (a PATH `stat` shim scoped to one field
and one operand suffix), driving the production `bash sutando-migrate.sh
--json` entry. Each unknown case is paired with a readable control on the same
fixtures, so a refusal-blind run cannot pass vacuously.

Run: python3 tests/sutando-migrate-json-unknown-metadata.test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATE = REPO / "scripts" / "sutando-migrate.sh"

REL = "notes/xsrc-collision.md"

# Refuse ONE stat field (mtime or size) for operands ending in the target
# suffix; everything else falls through to the real stat.
SHIM = """#!/bin/bash
want=0; hit=0
case "$STAT_SHIM_FIELD" in
    mtime) pat1=%m; pat2=%Y ;;
    size)  pat1=%z; pat2=%s ;;
esac
for a in "$@"; do
    case "$a" in
        "$pat1"|"$pat2") want=1 ;;
    esac
    case "$a" in
        *"$STAT_SHIM_TARGET") hit=1 ;;
    esac
done
if [ "$want" = 1 ] && [ "$hit" = 1 ]; then
    echo "refused $STAT_SHIM_FIELD: $*" >> "$STAT_SHIM_LOG"
    exit 9
fi
exec /usr/bin/stat "$@"
"""


class JsonUnknownMetadataIsNeverIdentical(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mig-json-unk-"))
        for leaf in ("src/a", "src/b", "dest", "home", "shim"):
            (self.tmp / leaf).mkdir(parents=True, exist_ok=True)
        self.shim_log = self.tmp / "stat-shim.log"
        shim = self.tmp / "shim" / "stat"
        shim.write_text(SHIM)
        shim.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, body_a: str, body_b: str, *, same_mtime: bool) -> None:
        now = time.time()
        for tag, body, age in (("a", body_a, 0), ("b", body_b, 600)):
            f = self.tmp / "src" / tag / REL
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
            ts = now if same_mtime else now - age
            os.utime(f, (ts, ts))

    def _scan(self, *, shim_field: str | None) -> dict:
        env = dict(os.environ)
        env.update(
            HOME=str(self.tmp / "home"),
            SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
            SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
            SUTANDO_MIGRATE_SRC_B=str(self.tmp / "src/b"),
            SUTANDO_MIGRATE_SRC_C=str(self.tmp / "absent-c"),
            STAT_SHIM_LOG=str(self.shim_log),
        )
        if shim_field is not None:
            env["STAT_SHIM_FIELD"] = shim_field
            env["STAT_SHIM_TARGET"] = "/" + REL.rsplit("/", 1)[-1]
            env["PATH"] = f"{self.tmp / 'shim'}{os.pathsep}{env['PATH']}"
        proc = subprocess.run(
            ["bash", str(MIGRATE), "--json"],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return json.loads(proc.stdout)

    def _row(self, out: dict) -> dict:
        rows = [r for r in out["notable_collisions"] if r["rel"] == REL]
        self.assertEqual(len(rows), 1, out["notable_collisions"])
        return rows[0]

    def _refusals(self) -> int:
        if not self.shim_log.exists():
            return 0
        return len(self.shim_log.read_text().splitlines())

    # ---- mtime unavailable ---------------------------------------------
    def test_unknown_mtime_same_size_is_not_identical(self) -> None:
        self._seed("AAAAA\n", "BBBBB\n", same_mtime=False)
        out = self._scan(shim_field="mtime")
        self.assertGreater(self._refusals(), 0, "shim never refused a probe")
        t = out["totals"]
        self.assertEqual(t["identical_content"], 0, t)
        self.assertEqual(t["unknown_metadata"], 1, t)
        self.assertEqual(t["genuine_conflicts"], 1, t)
        row = self._row(out)
        self.assertTrue(row["unknown_metadata"], row)
        for e in row["entries"]:
            self.assertIsNone(e["mtime"], e)
            self.assertEqual(e["size"], 6, e)  # size probes stayed live

    def test_CONTROL_readable_identical_still_reports_identical(self) -> None:
        self._seed("AAAAA\n", "AAAAA\n", same_mtime=True)
        out = self._scan(shim_field=None)
        self.assertEqual(self._refusals(), 0)
        t = out["totals"]
        self.assertEqual(t["identical_content"], 1, t)
        self.assertEqual(t["unknown_metadata"], 0, t)
        self.assertEqual(t["genuine_conflicts"], 0, t)
        self.assertFalse(self._row(out)["unknown_metadata"])

    # ---- size unavailable ----------------------------------------------
    def test_unknown_size_same_mtime_is_not_identical(self) -> None:
        self._seed("AA\n", "AAAAA\n", same_mtime=True)
        out = self._scan(shim_field="size")
        self.assertGreater(self._refusals(), 0, "shim never refused a probe")
        t = out["totals"]
        self.assertEqual(t["identical_content"], 0, t)
        self.assertEqual(t["unknown_metadata"], 1, t)
        self.assertEqual(t["genuine_conflicts"], 1, t)
        row = self._row(out)
        self.assertTrue(row["unknown_metadata"], row)
        for e in row["entries"]:
            self.assertIsNone(e["size"], e)
            self.assertIsNotNone(e["mtime"], e)  # mtime probes stayed live

    def test_CONTROL_readable_size_mismatch_is_genuine(self) -> None:
        self._seed("AA\n", "AAAAA\n", same_mtime=True)
        out = self._scan(shim_field=None)
        t = out["totals"]
        self.assertEqual(t["identical_content"], 0, t)
        self.assertEqual(t["size_mismatch"], 1, t)
        self.assertEqual(t["unknown_metadata"], 0, t)
        self.assertEqual(t["genuine_conflicts"], 1, t)


if __name__ == "__main__":
    unittest.main(verbosity=2)

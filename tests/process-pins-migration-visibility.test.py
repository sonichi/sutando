#!/usr/bin/env python3
"""A pin that survives migration as bytes but not as PROTECTION.

`state/process-pins.json` was classed `structural`, so a collision left one
version canonical and moved the other to a `.legacy-*` sidecar. The only
runtime reader (src/health-check.py, via process_pins.load_pins) loads the
canonical path and never reads sidecars — so a live pin could migrate into a
file nothing opens, the stale probe would prescribe a restart, and the 30-min
`--fix` cycle would kill the pinned process. That is the exact failure the pin
exists to prevent.

A class-only assertion cannot see this: `structural` and `union-json-array`
both "preserve the bytes". This drives the real migration script and then the
real reader, which is the only thing that distinguishes them.

Run: python3 tests/process-pins-migration-visibility.test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import process_pins as pp  # noqa: E402

SERVICE = "discord-bridge"
LIVE_PID, LIVE_LSTART = "222", "Sun Aug 24 09:11:02 2026"
DEAD_PID = "111"
FUTURE = "2099-01-01T00:00:00Z"


def pin(pid, lstart, reason):
    return {"service": SERVICE, "pid": int(pid), "lstart": lstart,
            "reason": reason, "expires_at": FUTURE}


class PinMigrationVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pin-migrate-"))
        for leaf in ("src/a", "src/b", "src/c", "dest", "home"):
            (self.tmp / leaf).mkdir(parents=True)
        # A is NEWER with a DEAD pin, C older with the LIVE one: newest-mtime
        # picks A, structural sidecars C, and either way the live pin is unread.
        self._write("src/a", pin(DEAD_PID, "Sat Aug 23 01:00:00 2026", "stale witness"), mtime=2_000_000_000)
        self._write("src/c", pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed"), mtime=1_000_000_000)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, leaf, one_pin, mtime):
        d = self.tmp / leaf / "state"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "process-pins.json"
        f.write_text(json.dumps({"pins": [one_pin]}, indent=2))
        os.utime(f, (mtime, mtime))
        return f

    def _migrate(self):
        env = dict(os.environ)
        env.update(HOME=str(self.tmp / "home"),
                   SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
                   SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
                   SUTANDO_MIGRATE_SRC_B=str(self.tmp / "src/b"),
                   SUTANDO_MIGRATE_SRC_C=str(self.tmp / "src/c"))
        r = subprocess.run(
            ["bash", str(REPO / "scripts" / "sutando-migrate.sh"), "--commit",
             "--no-confirm", "--no-claude-import", "--no-hook-bridge",
             "--no-channel-bridge"],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(r.returncode, 0, f"migrate failed:\n{r.stdout}\n{r.stderr}")
        return r

    def _verdict(self, pins_path):
        """Drive the real reader exactly as check_bridges does."""
        results = pp.evaluate(pp.load_pins(pins_path), SERVICE,
                              {LIVE_PID: LIVE_LSTART}, now_ts=0.0)
        return pp.stale_verdict(results, age_min=42)

    # Control first. Without it every assertion below could pass on a harness
    # that never reproduced the loss it claims to fix.
    def test_NEGATIVE_CONTROL_the_losing_source_alone_loses_protection(self) -> None:
        """A canonical record holding only A's pin prescribes a restart."""
        status, detail = self._verdict(self.tmp / "src/a" / "state" / "process-pins.json")
        self.assertEqual(status, "stale", detail)
        self.assertIn("restart needed", detail)

    def test_migration_keeps_every_pin_in_the_record_the_reader_reads(self) -> None:
        self._migrate()
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertTrue(canonical.exists(), "canonical pin file missing after migrate")
        pids = sorted(str(p.get("pid")) for p in pp.load_pins(canonical))
        self.assertEqual(pids, [DEAD_PID, LIVE_PID],
                         f"pins lost in migration: {pids}")

    def test_no_pin_is_stranded_in_an_unread_sidecar(self) -> None:
        self._migrate()
        state = self.tmp / "dest" / "state"
        strays = [p.name for p in state.glob("process-pins.json.legacy-*")]
        self.assertEqual(strays, [], f"pin stranded where no reader looks: {strays}")

    def test_the_live_pin_still_suppresses_the_restart_after_migration(self) -> None:
        """The behavioral claim: protection survives, not just the bytes."""
        self._migrate()
        status, detail = self._verdict(self.tmp / "dest" / "state" / "process-pins.json")
        self.assertEqual(status, "warn", detail)
        self.assertIn(f"DO NOT RESTART {SERVICE} pid {LIVE_PID}", detail)
        # The dead pin is not silently dropped either — it stays a finding.
        self.assertIn(DEAD_PID, detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)

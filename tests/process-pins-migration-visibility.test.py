#!/usr/bin/env python3
"""A pin is host-local, so migrating it is not preservation — it is resurrection.

`state/process-pins.json` records a LOCAL pid plus its `lstart`. On any other
host that pair names a different process or nothing at all, so the record has no
meaning off the machine that wrote it.

Two earlier classifications both failed, in opposite directions, and neither is
visible to a class-only assertion — both "preserve the bytes":

  structural         collision sidecars one copy to `.legacy-*`; the only runtime
                     reader (health-check via `process_pins.load_pins`) opens the
                     canonical path and never a sidecar, so a live pin migrated
                     into a file nothing reads.
  union-json-array   accumulates by record fingerprint, so a newer file can never
                     delete or supersede an older pin. An operator who RELEASES a
                     pin (the cleanup contract in `src/process_pins.py` prescribes
                     exactly that for orphan/mismatch/expired pins) gets it
                     re-armed by the next migration, and the `--fix` cycle then
                     keeps stale code running until the resurrected pin expires.

`skip-ephemeral` is the honest class, with a direct sibling precedent one screen
up in the same rule table: `state/cores/*.alive`, also per-host, also a local pid.

These drive the real migration script and then the real reader — the only thing
that separates "the bytes survived" from "the decision survived".

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

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, leaf, pins, mtime):
        d = self.tmp / leaf / "state"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "process-pins.json"
        f.write_text(json.dumps({"pins": pins}, indent=2))
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

    # ---- controls first: without these, every assertion below could pass on a
    # harness that never exercised the mechanism it claims to protect. ----

    def test_POSITIVE_CONTROL_a_local_armed_pin_still_suppresses_the_restart(self) -> None:
        """The pin mechanism itself is untouched by the reclassification."""
        f = self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], 1_000_000_000)
        status, detail = self._verdict(f)
        self.assertEqual(status, "warn", detail)
        self.assertIn(f"DO NOT RESTART {SERVICE} pid {LIVE_PID}", detail)

    def test_NEGATIVE_CONTROL_a_released_record_prescribes_a_restart(self) -> None:
        """An empty pin set is the operator saying 'you may restart now'."""
        f = self._write("src/a", [], 2_000_000_000)
        status, detail = self._verdict(f)
        self.assertEqual(status, "stale", detail)
        self.assertIn("restart needed", detail)

    # ---- the contract ----

    def test_migration_does_not_write_a_pin_file_at_the_destination(self) -> None:
        self._write("src/a", [pin(DEAD_PID, "Sat Aug 23 01:00:00 2026", "stale witness")], 2_000_000_000)
        self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], 1_000_000_000)
        self._migrate()
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertFalse(canonical.exists(),
                         "a host-local pin was migrated onto another host's record")

    def test_no_pin_is_stranded_in_an_unread_sidecar(self) -> None:
        self._write("src/a", [pin(DEAD_PID, "Sat Aug 23 01:00:00 2026", "stale witness")], 2_000_000_000)
        self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], 1_000_000_000)
        self._migrate()
        state = self.tmp / "dest" / "state"
        strays = [p.name for p in state.glob("process-pins.json*")] if state.exists() else []
        self.assertEqual(strays, [], f"pin written where it does not belong: {strays}")

    def test_migration_does_not_resurrect_a_RELEASED_pin(self) -> None:
        """The deletion-authority case: older armed pin, newer released record.

        Under `union-json-array` the destination ends up holding the older armed
        pin and the reader answers DO NOT RESTART, silently overriding an operator
        who had deliberately released it.
        """
        self._write("src/c", [pin(LIVE_PID, LIVE_LSTART, "#2604 witness armed")], 1_000_000_000)
        self._write("src/a", [], 2_000_000_000)          # released, and newer
        self._migrate()
        canonical = self.tmp / "dest" / "state" / "process-pins.json"
        self.assertFalse(canonical.exists(), "release was overridden by migration")
        # And a host whose own record is the released one keeps its own answer.
        status, detail = self._verdict(self.tmp / "src/a" / "state" / "process-pins.json")
        self.assertEqual(status, "stale", detail)
        self.assertIn("restart needed", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)

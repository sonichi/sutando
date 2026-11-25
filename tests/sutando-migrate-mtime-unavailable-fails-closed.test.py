#!/usr/bin/env python3
"""An unavailable mtime must never be read as epoch zero.

`_stat_field` used to print `${v:-0}` and return success, so a file whose BSD
*and* GNU mtime probes both failed compared as the oldest possible file. In the
rehome-state/dated-snapshot branch that let an older source overwrite a
genuinely newer destination, and let a genuinely newer source be discarded as
older -- both exiting 0 and writing the source sentinel, so an ordinary retry
skipped the file once the transient failure cleared.

Failure is injected at the real seam: a PATH shim whose `stat` exits non-zero
for BOTH mtime formats (%m, %Y) of exactly ONE operand. Size probes and every
other file's probes stay live, so this reproduces a transient per-file stat
failure rather than a broken `stat`.

Controls, without which the negatives below prove nothing:
  * the same fixtures WITHOUT the shim must migrate and preserve the newer
    destination, exiting 0 -- otherwise "dest unchanged" is vacuous;
  * the shim must record that it actually refused a probe.

Run: python3 tests/sutando-migrate-mtime-unavailable-fails-closed.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATE = REPO / "scripts" / "sutando-migrate.sh"

# SOURCE-relative: the classifier keys on the BARE name (migrate.sh:202). The
# already-homed path classifies `structural` and routes to the COLLISION branch.
REL = "cloud-auth.json"
DEST_REL = "state/auth/cloud-auth.json"   # where rehome-state lands it
REHOME_MARKER = "rehome-mtime-unavailable"
SRC_BODY = '{"token": "OLDER-SOURCE"}\n'
DEST_BODY = '{"token": "NEWER-DEST"}\n'

FORBIDDEN = ("rehomed-newer", "COMMIT complete")

SHIM = """#!/bin/bash
# Fail BOTH mtime probes (%m BSD, %Y GNU) for one operand only; size probes and
# other paths hit the real stat -- per-file, per-field, not a broken stat.
want_mtime=0; hit=0
for a in "$@"; do
    case "$a" in
        %m|%Y) want_mtime=1 ;;
    esac
    case "$a" in
        *"$STAT_SHIM_TARGET") hit=1 ;;
    esac
done
if [ "$want_mtime" = 1 ] && [ "$hit" = 1 ]; then
    echo "refused mtime: $*" >> "$STAT_SHIM_LOG"
    exit 9
fi
exec /usr/bin/stat "$@"
"""


class MtimeUnavailableFailsClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mig-mtime-"))
        for leaf in ("src/a", "dest", "home", "shim"):
            (self.tmp / leaf).mkdir(parents=True, exist_ok=True)
        self.shim_log = self.tmp / "stat-shim.log"
        shim = self.tmp / "shim" / "stat"
        shim.write_text(SHIM)
        shim.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self) -> None:
        """Destination is genuinely NEWER. Correct behavior keeps it."""
        now = time.time()
        src = self.tmp / "src/a" / REL
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(SRC_BODY)
        os.utime(src, (now - 600, now - 600))
        dst = self.tmp / "dest" / DEST_REL
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(DEST_BODY)
        os.utime(dst, (now, now))

    def _migrate(self, *, target: str | None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            HOME=str(self.tmp / "home"),
            SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
            SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
            SUTANDO_MIGRATE_SRC_B=str(self.tmp / "absent-b"),
            SUTANDO_MIGRATE_SRC_C=str(self.tmp / "absent-c"),
            STAT_SHIM_LOG=str(self.shim_log),
        )
        if target is not None:
            env["STAT_SHIM_TARGET"] = target
            env["PATH"] = f"{self.tmp / 'shim'}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["bash", str(MIGRATE), "--commit", "--no-confirm",
             "--no-claude-import", "--no-hook-bridge", "--no-channel-bridge"],
            capture_output=True, text=True, env=env, timeout=180)

    def _dest(self):
        p = self.tmp / "dest" / DEST_REL
        return p.read_text() if p.exists() else None

    def _sentinels(self):
        d = self.tmp / "dest" / "state"
        return sorted(p.name for p in d.glob(".migrated-from-A-*")) if d.exists() else []

    # ---- controls -------------------------------------------------------
    def test_CONTROL_without_shim_the_newer_destination_survives(self) -> None:
        self._seed()
        r = self._migrate(target=None)
        self.assertEqual(r.returncode, 0,
                         f"unshimmed migration failed; negatives below prove "
                         f"nothing:\n{r.stdout}{r.stderr}")
        self.assertEqual(self._dest(), DEST_BODY,
                         "control: newer destination was overwritten even with "
                         "both mtimes readable")

    # ---- reciprocal negatives -------------------------------------------
    def test_destination_mtime_unavailable_refuses(self) -> None:
        self._seed()
        r = self._migrate(target="dest/" + DEST_REL)
        self._assert_refused(r, "destination mtime unavailable")

    def test_source_mtime_unavailable_refuses(self) -> None:
        self._seed()
        r = self._migrate(target="src/a/" + REL)
        self._assert_refused(r, "source mtime unavailable")

    def _assert_refused(self, r, label: str) -> None:
        out = r.stdout + r.stderr
        self.assertTrue(self.shim_log.exists() and self.shim_log.read_text().strip(),
                        f"[{label}] the shim never refused a probe — the "
                        f"injection missed, so this negative is vacuous")
        self.assertNotEqual(r.returncode, 0,
                            f"[{label}] mtime was unavailable but the migrator "
                            f"exited 0:\n{out}")
        self.assertEqual(self._dest(), DEST_BODY,
                         f"[{label}] canonical bytes changed on an unknown "
                         f"mtime:\n{out}")
        self.assertEqual(self._sentinels(), [],
                         f"[{label}] a source sentinel was written, so the "
                         f"retry will skip this file:\n{out}")
        # Without this the test passes from the COLLISION branch and silently
        # stops covering the rehome path -- the defect it was written for.
        self.assertIn(REHOME_MARKER, out,
                      f"[{label}] refusal came from a branch other than rehome; "
                      f"this regression is not covering :1376-1388:\n{out}")
        for tok in FORBIDDEN:
            self.assertNotIn(tok, out,
                             f"[{label}] output claims success ({tok!r}):\n{out}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

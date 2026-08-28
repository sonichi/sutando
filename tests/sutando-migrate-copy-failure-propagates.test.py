#!/usr/bin/env python3
"""A failed copy must not be recorded as a successful migration.

`commit_one()` runs inside a command substitution on the left of an OR-list, so
bash disables errexit for its whole call tree. A `copy_preserving_mtime` that
fails there used to fall straight through to the success `echo` ("copied" /
"src-newer") and `return 0`; `commit_source` then wrote the per-source sentinel,
so every later run skipped the file ("prior migration sentinel — skip") and the
content was never migrated at all.

The boundary is `commit_copy` — every commit-path copy goes through it and every
failure returns explicitly, independent of `set -e` and caller context.

Both success paths that a lost byte can hide behind are exercised:

  fresh-copy   dest has no such file  -> the "copied" outcome
  replacement  src is newer than dest -> the "src-newer" outcome

Failure is injected at the real seam: a PATH shim whose `cp` exits non-zero for
the fixture file only, so the rest of the migration (backup tar, mkdir, mv) runs
untouched and the refusal cannot be an artifact of a broken environment.

Controls, because a negative assertion is only worth its coverage:
  * the same fixtures WITHOUT the shim must migrate, print "COMMIT complete" and
    write the sentinel -- otherwise "no sentinel" proves nothing;
  * the shim must record that it actually refused a copy -- otherwise the
    refusal under test could come from anywhere.

Run: python3 tests/sutando-migrate-copy-failure-propagates.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIGRATE = REPO / "scripts" / "sutando-migrate.sh"

REL = "state/process-pins.json"          # classified newest-mtime
SRC_BODY = '{"pins": [{"service": "discord-bridge", "pid": 222}]}\n'
DEST_BODY = '{"pins": []}\n'

# Success vocabulary the migrator must NOT emit when a copy failed.
FORBIDDEN = ("copied", "src-newer", "COMMIT complete")

SHIM = """#!/bin/bash
for a in "$@"; do
    case "$a" in
        *process-pins.json*)
            echo "refused: $*" >> "$CP_SHIM_LOG"
            exit 42
            ;;
    esac
done
exec /bin/cp "$@"
"""


class CopyFailurePropagatesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="migrate-copyfail-"))
        for leaf in ("src/a", "dest", "home", "shim"):
            (self.tmp / leaf).mkdir(parents=True)
        self.shim_log = self.tmp / "cp-shim.log"
        shim = self.tmp / "shim" / "cp"
        shim.write_text(SHIM)
        shim.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixtures -------------------------------------------------------
    def _seed(self, *, replacement: bool) -> None:
        """Source always carries the file; dest carries an OLDER copy only for
        the replacement case, which is what selects the `src-newer` branch."""
        now = time.time()
        src = self.tmp / "src/a" / REL
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(SRC_BODY)
        os.utime(src, (now, now))
        if replacement:
            dst = self.tmp / "dest" / REL
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(DEST_BODY)
            os.utime(dst, (now - 600, now - 600))

    # ---- runner ---------------------------------------------------------
    def _migrate(self, *, shim: bool) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            HOME=str(self.tmp / "home"),
            SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
            SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
            SUTANDO_MIGRATE_SRC_B=str(self.tmp / "absent-b"),
            SUTANDO_MIGRATE_SRC_C=str(self.tmp / "absent-c"),
            CP_SHIM_LOG=str(self.shim_log),
        )
        if shim:
            env["PATH"] = f"{self.tmp / 'shim'}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["bash", str(MIGRATE), "--commit", "--no-confirm",
             "--no-claude-import", "--no-hook-bridge", "--no-channel-bridge"],
            capture_output=True, text=True, env=env, timeout=180)

    # ---- observations ---------------------------------------------------
    def _sentinels(self):
        return sorted(p.name for p in (self.tmp / "dest" / "state").glob(".migrated-from-A-*"))

    def _dest_state(self):
        p = self.tmp / "dest" / REL
        return p.read_text() if p.exists() else None

    def _assert_refused(self, r, *, dest_before, label):
        out = r.stdout + r.stderr
        self.assertNotEqual(
            r.returncode, 0,
            f"[{label}] copy failed but the migrator exited 0:\n{out}")
        self.assertEqual(
            self._dest_state(), dest_before,
            f"[{label}] destination changed despite the copy failing:\n{out}")
        self.assertEqual(
            self._sentinels(), [],
            f"[{label}] a source sentinel was written after a failed copy — "
            f"every retry now skips the file:\n{out}")
        for token in FORBIDDEN:
            self.assertNotIn(
                token, out,
                f"[{label}] output claims success ({token!r}) after a failed copy:\n{out}")

    # ---- controls -------------------------------------------------------
    def test_CONTROL_fixtures_migrate_when_cp_works(self) -> None:
        """Without the shim both fixtures must succeed, or the negatives below
        are vacuous: they would hold for a migration that never ran."""
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                self.setUp()
                try:
                    self._seed(replacement=replacement)
                    r = self._migrate(shim=False)
                    out = r.stdout + r.stderr
                    self.assertEqual(r.returncode, 0, f"control migration failed:\n{out}")
                    self.assertIn("COMMIT complete", out)
                    self.assertEqual(self._dest_state(), SRC_BODY,
                                     "control did not land the source content")
                    self.assertNotEqual(self._sentinels(), [],
                                        "control wrote no sentinel — the sentinel "
                                        "assertion cannot discriminate")
                finally:
                    self.tearDown()

    def test_CONTROL_shim_actually_refuses_a_copy(self) -> None:
        """The injected failure must fire; otherwise the refusals under test
        could be caused by anything else in the run."""
        self._seed(replacement=False)
        self._migrate(shim=True)
        self.assertTrue(self.shim_log.exists() and self.shim_log.read_text().strip(),
                        "the cp shim never refused a copy — the injection did not fire")

    # ---- the defect -----------------------------------------------------
    def test_fresh_copy_failure_is_not_recorded_as_migrated(self) -> None:
        self._seed(replacement=False)
        r = self._migrate(shim=True)
        self._assert_refused(r, dest_before=None, label="fresh-copy")

    def test_replacement_copy_failure_is_not_recorded_as_migrated(self) -> None:
        self._seed(replacement=True)
        r = self._migrate(shim=True)
        self._assert_refused(r, dest_before=DEST_BODY, label="src-newer")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Regression tests for sutando-migrate.sh argument aliases."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MIGRATE = ROOT / "scripts" / "sutando-migrate.sh"


class TestSutandoMigrateArgparse(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sutando-migrate-args-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dash_dash_commit_alias_enters_commit_mode(self) -> None:
        """startup.sh and operator docs use --commit; keep it accepted."""
        dest = self.tmp / "dest"
        source = self.tmp / "source-a"
        (source / "notes").mkdir(parents=True)
        (source / "notes" / "argparse.md").write_text("alias smoke\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "SUTANDO_MIGRATE_DEST": str(dest),
                "SUTANDO_MIGRATE_SRC_A": str(source),
                "SUTANDO_MIGRATE_SRC_B": str(self.tmp / "missing-b"),
                "SUTANDO_MIGRATE_SRC_C": str(self.tmp / "missing-c"),
            }
        )
        env.pop("SUTANDO_WORKSPACE", None)

        proc = subprocess.run(
            ["bash", str(MIGRATE), "--commit", "--source", "A"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("sutando-migrate: COMMIT mode", combined)
        self.assertNotIn("unknown arg: --commit", combined)
        self.assertTrue((dest / "notes" / "argparse.md").is_file())


if __name__ == "__main__":
    unittest.main()

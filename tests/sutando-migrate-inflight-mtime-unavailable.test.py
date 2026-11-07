#!/usr/bin/env python3
"""An unavailable mtime on an in-flight-class file must refuse, not complete.

`age_safe` returned 1 both for "younger than the guard" and for "_stat_field
mtime failed", the inflight-guard branch read either as `skipped-inflight`
with rc 0, and `commit_source` then wrote the completion sentinel — so a
transient stat failure became a completed migration, and an ordinary retry
refused on the sentinel ("prior migration sentinel — skip"). The record was
silently omitted from the canonical archive with no repair path short of
--force.

Failure is injected at the real seam (a PATH `stat` shim refusing only the
mtime probes of only the target operand) through the production
`bash sutando-migrate.sh --commit` entry. The readable control, the refusal,
and the restored-stat retry are all asserted, plus the scan's size renderer
(an unknown size must not print as a measured 0).

Run: python3 tests/sutando-migrate-inflight-mtime-unavailable.test.py
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

REL = "tasks/task-chat-1700000000.txt"   # classifies inflight-guard
BODY = "id: task-chat-1700000000\ntask: old work\n"

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


class InflightMtimeUnavailableRefuses(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mig-inflight-unk-"))
        for leaf in ("src/a", "dest", "home", "shim"):
            (self.tmp / leaf).mkdir(parents=True, exist_ok=True)
        self.shim_log = self.tmp / "stat-shim.log"
        shim = self.tmp / "shim" / "stat"
        shim.write_text(SHIM)
        shim.chmod(0o755)
        src = self.tmp / "src/a" / REL
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(BODY)
        old = time.time() - 3600   # well past the 60s in-flight guard
        os.utime(src, (old, old))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *, shim_field: str | None, mode: str = "--commit") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(
            HOME=str(self.tmp / "home"),
            SUTANDO_MIGRATE_DEST=str(self.tmp / "dest"),
            SUTANDO_MIGRATE_SRC_A=str(self.tmp / "src/a"),
            SUTANDO_MIGRATE_SRC_B=str(self.tmp / "absent-b"),
            SUTANDO_MIGRATE_SRC_C=str(self.tmp / "absent-c"),
            STAT_SHIM_LOG=str(self.shim_log),
        )
        if shim_field is not None:
            env["STAT_SHIM_FIELD"] = shim_field
            env["STAT_SHIM_TARGET"] = "/" + REL.rsplit("/", 1)[-1]
            env["PATH"] = f"{self.tmp / 'shim'}{os.pathsep}{env['PATH']}"
        args = ["bash", str(MIGRATE)]
        if mode:
            args.append(mode)
        if mode == "--commit":
            args += ["--no-confirm", "--no-claude-import",
                     "--no-hook-bridge", "--no-channel-bridge"]
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=180)

    def _archived(self) -> bool:
        return (self.tmp / "dest" / "tasks" / "archive" / "A"
                / Path(REL).name).exists()

    def _sentinels(self) -> list:
        d = self.tmp / "dest" / "state"
        return sorted(p.name for p in d.glob(".migrated-from-A-*")) if d.exists() else []

    def _refusals(self) -> int:
        if not self.shim_log.exists():
            return 0
        return len(self.shim_log.read_text().splitlines())

    def test_full_cycle_refuse_then_retry_succeeds(self) -> None:
        # CONTROL variant runs in test_CONTROL below; ordering here is the
        # drill itself: refusal first, then the retry with stat restored.
        p1 = self._run(shim_field="mtime")
        self.assertGreater(self._refusals(), 0, "shim never refused a probe")
        self.assertNotEqual(p1.returncode, 0,
                            "mtime-unavailable commit must refuse\n" + p1.stderr[-1500:])
        self.assertIn("mtime unavailable", p1.stderr)
        self.assertFalse(self._archived(), "refused file must not be archived")
        self.assertEqual(self._sentinels(), [],
                         "NO sentinel may be written on a refused commit")
        # Retry with the real stat: must succeed and archive the old task.
        p2 = self._run(shim_field=None)
        self.assertEqual(p2.returncode, 0, p2.stderr[-1500:])
        self.assertTrue(self._archived(), "retry must archive the stale task")
        self.assertNotEqual(self._sentinels(), [], "retry writes the sentinel")

    def test_CONTROL_readable_old_file_archives(self) -> None:
        p = self._run(shim_field=None)
        self.assertEqual(self._refusals(), 0)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertTrue(self._archived())
        self.assertNotEqual(self._sentinels(), [])

    def test_scan_unknown_size_is_not_a_measured_zero(self) -> None:
        p = self._run(shim_field="size", mode="")
        self.assertGreater(self._refusals(), 0, "shim never refused a probe")
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertIn("unknown size", p.stdout,
                      "scan must say a size was unavailable\n" + p.stdout[-1500:])

    def test_CONTROL_scan_readable_size_is_counted(self) -> None:
        p = self._run(shim_field=None, mode="")
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertNotIn("unknown size", p.stdout)
        self.assertIn("total bytes:", p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

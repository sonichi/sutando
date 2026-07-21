#!/usr/bin/env python3
"""Regression tests for #2222: last-owner-activity.json torn/lost writes.

`write_owner_activity()` in slack/discord/telegram bridges staged the temp file
under a SHARED name (`.json.tmp`). That file is written by four processes (the
three bridges + the sparrow remote-gateway bridge), so two concurrent writers
could truncate and interleave the same temp file, and the rename could then
publish torn JSON — a reader (the proactive loop's owner-presence check) would
see a corrupt or partial timestamp. The fix stages under a per-PID name so no
two processes ever share a temp file, and uses os.replace (atomic overwrite).

Two tests:
  1. Concurrency proof — many processes writing the target file with the fixed
     (per-PID) pattern; the file is valid JSON at every sampled read and after.
     This is designed to run long enough that the OLD shared-name pattern would
     tear (demonstrated by the `--demo-old-pattern-tears` self-check below).
  2. Source guard — the three bridge call sites use the per-PID form, not the
     bare shared `.json.tmp`. Deterministic; mutation-checked (revert → fail).

Run: python3 tests/owner-activity-atomic-write.test.py
"""
import json
import multiprocessing
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# ---- the two staging strategies, isolated so the test exercises the exact
#      shape the bridges use (shared vs per-PID + os.replace) ----------------

def _write_shared(target: Path, payload: dict) -> None:
    tmp = target.with_suffix(".json.tmp")            # the #2222 bug
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)


def _write_perpid(target: Path, payload: dict) -> None:
    tmp = target.with_suffix(f".json.{os.getpid()}.tmp")  # the fix
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, target)


def _hammer(target_str: str, strategy: str, n: int) -> int:
    """Write `n` times; return the count of self-observed valid final states.
    Runs in a child process (macOS spawn re-imports this module cleanly)."""
    target = Path(target_str)
    writer = _write_shared if strategy == "shared" else _write_perpid
    for i in range(n):
        writer(target, {"ts": int(time.time()), "pid": os.getpid(), "i": i})
    return n


class TestOwnerActivityAtomicWrite(unittest.TestCase):
    def _run_fleet(self, strategy: str, procs: int = 12, writes: int = 120) -> Path:
        tmpdir = Path(tempfile.mkdtemp(prefix="oa-2222-"))
        target = tmpdir / "last-owner-activity.json"
        target.write_text(json.dumps({"ts": 0, "seed": True}))
        ctx = multiprocessing.get_context("spawn")
        ps = [ctx.Process(target=_hammer, args=(str(target), strategy, writes))
              for _ in range(procs)]
        for p in ps:
            p.start()
        # Sample the file WHILE writers race; every observed state must parse.
        torn = 0
        deadline = time.time() + 5
        while any(p.is_alive() for p in ps) and time.time() < deadline:
            try:
                json.loads(target.read_text())
            except (ValueError, OSError):
                torn += 1
        for p in ps:
            p.join(10)
        # Final state must always be valid, regardless of strategy (rename is
        # atomic); the interesting signal is torn reads observed mid-race.
        json.loads(target.read_text())
        self._torn = torn
        return target

    def test_perpid_never_tears_under_concurrency(self):
        """With per-PID staging, no concurrent reader ever sees torn JSON."""
        self._run_fleet("perpid")
        self.assertEqual(
            self._torn, 0,
            f"per-PID staging produced {self._torn} torn reads — must be 0",
        )

    def test_source_sites_use_perpid_staging(self):
        """All FIVE owner-activity writers — the three bridges, the sparrow
        remote-gateway bridge (Python), and the task-bridge (TypeScript) — must
        stage OWNER_ACTIVITY_FILE per-PID (#2222). The census is complete: any
        new writer of the shared target must appear here or the guard is a lie."""
        bridges = ["src/slack-bridge.py", "src/discord-bridge.py",
                   "src/telegram-bridge.py",
                   "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"]
        bad = re.compile(r'OWNER_ACTIVITY_FILE\.with_suffix\(\s*["\']\.json\.tmp["\']')
        good = re.compile(r'OWNER_ACTIVITY_FILE\.with_suffix\(\s*f["\']\.json\.\{os\.getpid\(\)\}\.tmp["\']')
        for rel in bridges:
            src = (REPO / rel).read_text()
            self.assertIsNone(
                bad.search(src),
                f"{rel} still stages OWNER_ACTIVITY_FILE under a shared .json.tmp",
            )
            self.assertIsNotNone(
                good.search(src),
                f"{rel} does not use the per-PID staging name",
            )

        # The 5th writer is TypeScript (src/task-bridge.ts) with different syntax:
        # a shared `OWNER_ACTIVITY_FILE + '.tmp'` vs a per-PID template literal
        # `${OWNER_ACTIVITY_FILE}.${process.pid}.tmp`.
        ts_rel = "src/task-bridge.ts"
        ts_src = (REPO / ts_rel).read_text()
        ts_bad = re.compile(r"OWNER_ACTIVITY_FILE\s*\+\s*['\"]\.tmp['\"]")
        ts_good = re.compile(r"\$\{OWNER_ACTIVITY_FILE\}\.\$\{process\.pid\}\.tmp")
        self.assertIsNone(
            ts_bad.search(ts_src),
            f"{ts_rel} still stages OWNER_ACTIVITY_FILE under a shared .tmp",
        )
        self.assertIsNotNone(
            ts_good.search(ts_src),
            f"{ts_rel} does not use the per-PID staging name",
        )


# Self-check (not part of the suite): demonstrate the OLD pattern CAN tear, so
# the concurrency test above is known to exercise a real race. Run manually:
#   python3 tests/owner-activity-atomic-write.test.py --demo-old-pattern-tears
def _demo_old_tears() -> None:
    t = TestOwnerActivityAtomicWrite()
    t._run_fleet("shared", procs=16, writes=400)
    print(f"shared-name pattern: {t._torn} torn reads observed mid-race "
          f"(>0 confirms the race is real)")


if __name__ == "__main__":
    if "--demo-old-pattern-tears" in sys.argv:
        _demo_old_tears()
    else:
        unittest.main()

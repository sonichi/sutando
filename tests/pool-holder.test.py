#!/usr/bin/env python3
"""Unit tests for src/pool_holder.py — the optional-skill lookup that says
which pool worker holds a queued task (shared by unanswered-tasks and cron-gate)."""
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("pool_holder", REPO / "src" / "pool_holder.py")
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILURES.append(name)


def cli(*args):
    p = subprocess.run([sys.executable, str(REPO / "src" / "pool_holder.py"), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "ws"
        (ws / "tasks").mkdir(parents=True)
        tid = "task-1786301397837"
        (ws / "tasks" / f"{tid}.txt").write_text("id: t\naccess_tier: owner\ntask: x\n")

        check("no deliveries/ at all means nobody holds it", ph.pool_holder(ws, tid) is None)
        (ws / "deliveries").mkdir()
        check("an empty deliveries/ means nobody holds it", ph.pool_holder(ws, tid) is None)

        (ws / "deliveries" / "w1").mkdir()
        (ws / "deliveries" / "w1" / f"{tid}.txt").write_text("")
        check("a pending sentinel names its worker", ph.pool_holder(ws, tid) == "w1")
        (ws / "deliveries" / "w1" / f"{tid}.txt").unlink()
        (ws / "deliveries" / "w1" / f"{tid}.accepted").write_text("")
        check("an accepted sentinel names its worker too", ph.pool_holder(ws, tid) == "w1")
        check("another task is not held", ph.pool_holder(ws, "task-999") is None)

        empty = Path(td) / "no-skill"
        empty.mkdir()
        check("an absent worker-pool skill means nobody holds anything",
              ph.pool_holder(ws, tid, pool_scripts=empty) is None)
        check("a nonexistent skill dir is absent, not an error",
              ph.pool_holder(ws, tid, pool_scripts=Path(td) / "missing") is None)

        rc, out, _ = cli(str(ws), tid)
        check("CLI prints the holder and exits 0", (rc, out) == (0, "w1"), (rc, out))
        rc, out, _ = cli(str(ws), str(ws / "tasks" / f"{tid}.txt"))
        check("CLI accepts the task file path", (rc, out) == (0, "w1"), (rc, out))
        rc, out, _ = cli(str(ws), "task-999")
        check("CLI exits 1 with no output when nobody holds it", (rc, out) == (1, ""), (rc, out))
        rc, out, _ = cli(str(ws), tid, "--pool-scripts", str(empty))
        check("CLI exits 1 when the skill is absent", rc == 1, rc)

        if os.geteuid() != 0:
            (ws / "deliveries").chmod(0)
            try:
                try:
                    ph.pool_holder(ws, tid)
                    check("an unreadable deliveries/ raises, never reads as nobody", False)
                except OSError:
                    check("an unreadable deliveries/ raises, never reads as nobody", True)
                rc, out, err = cli(str(ws), tid)
                check("CLI exits 3 (undecidable) on an unreadable deliveries/",
                      rc == 3 and "cannot read deliveries/" in err, (rc, err))
            finally:
                (ws / "deliveries").chmod(stat.S_IRWXU)

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())

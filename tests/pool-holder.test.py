#!/usr/bin/env python3
"""Unit tests for src/pool_holder.py — the optional-skill lookup that says
which pool worker holds a queued task (shared by unanswered-tasks and cron-gate)."""
import contextlib
import importlib.util
import io
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


def run_main(*args):
    """main() in-process, so the coverage gate measures the CLI's own lines."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ph.main(list(args))
    return rc, out.getvalue().strip(), err.getvalue().strip()


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

        rc, out, err = run_main(str(ws), tid)
        check("main prints the holder and returns 0", (rc, out, err) == (0, "w1", ""), (rc, out, err))
        rc, out, _ = run_main(str(ws), str(ws / "tasks" / f"{tid}.txt"))
        check("main strips the directory and .txt from a task path", (rc, out) == (0, "w1"), (rc, out))
        rc, out, err = run_main(str(ws), "task-999")
        check("main returns 1 and prints nothing when nobody holds it",
              (rc, out, err) == (1, "", ""), (rc, out, err))
        rc, out, _ = run_main(str(ws), tid, "--pool-scripts", str(empty))
        check("main returns 1 when the skill is absent", (rc, out) == (1, ""), (rc, out))
        # A skill dir under a regular file stats as ENOTDIR: unreadable, not absent.
        blocked = Path(td) / "a-file"
        blocked.write_text("")
        try:
            ph.pool_holder(ws, tid, pool_scripts=blocked / "scripts")
            check("an unreadable skill dir raises, never reads as absent", False)
        except NotADirectoryError:
            check("an unreadable skill dir raises, never reads as absent", True)
        rc, out, err = run_main(str(ws), tid, "--pool-scripts", str(blocked / "scripts"))
        check("main returns 3 (undecidable) and prints no holder on an OSError",
              rc == 3 and out == "" and err.startswith("pool-holder: cannot read"), (rc, out, err))

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

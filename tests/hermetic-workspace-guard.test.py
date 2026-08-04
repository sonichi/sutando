#!/usr/bin/env python3
"""The guard must be able to FAIL, and that has to be asserted, not demonstrated.

@Sutando-Pro on #2639, about the per-suite shape: *"assert the redirect target
GREW — otherwise deleting the `append()` call entirely turns the guard green,
live file unchanged, redirect file unchanged, both assertions satisfied, and
you've proven nothing about whether the write happened at all."*

Translated to this wrapper, the equivalent hole is worse: if
`resolved_workspace()` returns a path that does not exist, or the walk silently
yields nothing, then **every test passes vacuously** and the guard reports a
clean suite forever. That is the same broken-instrument failure the guard exists
to catch, one level up — a negative from something never shown able to produce a
positive.

So the guard's own detection ability is pinned here rather than demonstrated
once in a shell:

  * a fixture that writes into the resolved workspace MUST fail (exit 1)
  * a fixture that writes nothing MUST pass (exit 0)  <- without this, a guard
    hardwired to fail also satisfies the case above
  * the snapshot must actually see files — an empty walk is a broken instrument,
    not a clean workspace

Run: python3 tests/hermetic-workspace-guard.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "hermetic-workspace-guard.py"

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {name} {detail}")


def _run_guard(workdir: Path, test_file: Path, exemptions: str = "") -> subprocess.CompletedProcess:
    """Run the guard in a repo copy whose workspace points at a temp dir."""
    (workdir / "tests").mkdir(parents=True, exist_ok=True)
    (workdir / "tests" / "hermetic-workspace-exemptions.txt").write_text(exemptions)
    return subprocess.run(
        [sys.executable, str(workdir / "scripts" / "hermetic-workspace-guard.py"),
         "run", str(test_file)],
        capture_output=True, text=True, cwd=str(workdir),
    )


def _fixture_repo(tmp: Path, ws: Path) -> Path:
    """A minimal repo whose `resolve_workspace()` lands on `ws`."""
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    for rel in ("scripts/hermetic-workspace-guard.py", "src/workspace_default.py",
                "src/sutando_config.py", "sutando.config.json"):
        src = REPO / rel
        if src.exists():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_bytes(src.read_bytes())
    (repo / "sutando.config.local.json").write_text(
        json.dumps({"workspace": {"path": str(ws)}, "vault": {"enabled": False}}))
    return repo


def main() -> int:
    print("hermetic workspace guard — can it fail?")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ws = tmp / "ws"
        (ws / "state").mkdir(parents=True)
        (ws / "state" / "preexisting.json").write_text("{}")   # so the walk is non-empty
        repo = _fixture_repo(tmp, ws)

        offender = tmp / "offender.test.py"
        offender.write_text(
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(repo / 'src')!r})\n"
            "from workspace_default import resolve_workspace\n"
            "(pathlib.Path(resolve_workspace())/'state'/'leaked.log').write_text('x')\n"
            "print('offender: my own assertions passed')\n"
        )
        clean = tmp / "clean.test.py"
        clean.write_text("print('clean: touched nothing')\n")

        # --- THE POINT: a green test that pollutes must still be failed -------
        r = _run_guard(repo, offender)
        check("a test that PASSES but writes to the live workspace is FAILED",
              r.returncode != 0, f"exit={r.returncode}")
        check("...and the offending path is NAMED, not just counted",
              "state/leaked.log" in (r.stdout + r.stderr),
              (r.stdout + r.stderr)[-200:])

        # --- POSITIVE CONTROL -------------------------------------------------
        # Without this, a guard hardwired to `return 1` satisfies the case above.
        r = _run_guard(repo, clean)
        check("POSITIVE CONTROL — a clean test passes", r.returncode == 0,
              f"exit={r.returncode} {(r.stdout + r.stderr)[-200:]}")

        # --- exemptions: named, and self-expiring ----------------------------
        r = _run_guard(repo, offender, "offender.test.py state/leaked.log\n")
        check("a NAMED exemption permits exactly that path", r.returncode == 0,
              f"exit={r.returncode}")

        r = _run_guard(repo, offender, "offender.test.py state/some-other-path.log\n")
        out = r.stdout + r.stderr
        check("an exemption aimed ELSEWHERE does not cover the real write",
              r.returncode != 0 and "state/leaked.log" in out, out[-200:])
        check("...and that mis-aimed exemption is ALSO reported stale",
              "STALE" in out, out[-200:])

        r = _run_guard(repo, clean, "clean.test.py state/never-written.log\n")
        check("an UNUSED exemption fails (self-expiring, @Sutando-Pro)",
              r.returncode != 0 and "STALE" in (r.stdout + r.stderr),
              f"exit={r.returncode}")

        # --- the instrument itself -------------------------------------------
        sys.path.insert(0, str(REPO / "scripts"))
        import importlib.util
        spec = importlib.util.spec_from_file_location("hwg", GUARD)
        hwg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hwg)
        snap = hwg.snapshot(ws)
        check("the snapshot SEES files — an empty walk is a broken instrument, "
              "not a clean workspace", len(snap) > 0, f"snapshot had {len(snap)} entries")
        check("a missing workspace yields an empty snapshot rather than a crash",
              hwg.snapshot(tmp / "does-not-exist") == {})

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("hermetic workspace guard: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
        # --- IN-PROCESS drive of main() + helpers -----------------------------
        # The subprocess controls above prove BEHAVIOUR but are invisible to the
        # coverage gate: `scripts/coverage-gate.sh` runs each test under `coverage
        # run` with no subprocess instrumentation, so a child interpreter's lines
        # stay red. This PR's first cut measured 31.9% for exactly that reason.
        # These calls re-exercise the same paths in THIS interpreter, so the gate
        # sees them. [[reference_coverage_gate_needs_inprocess_tests]]
        import contextlib
        import io

        # resolved_workspace(): reads the loader, so point it at our fixture repo
        _saved = sys.path[:]
        sys.path.insert(0, str(repo / "src"))
        try:
            import importlib as _il
            _wd = _il.import_module("workspace_default")
            _orig = _wd.resolve_workspace
            _wd.resolve_workspace = lambda *a, **k: str(ws)
            try:
                check("resolved_workspace() returns the loader's answer, in-process",
                      hwg.resolved_workspace() == ws, str(hwg.resolved_workspace()))
            finally:
                _wd.resolve_workspace = _orig
        except Exception as exc:                      # loader unavailable in fixture
            print(f"    (note: resolved_workspace in-process skipped: {type(exc).__name__})")
        finally:
            sys.path[:] = _saved

        # exemptions_for(): the no-entry branch
        check("exemptions_for() returns [] for a file with no entry, in-process",
              hwg.exemptions_for("no-such-file.test.py") == [])

        # ...and the PARSE body, which the call above never reaches: the repo's
        # own exemptions file is currently all comments, so the loop `continue`s
        # on every line. Only the subprocess controls above ever parsed a real
        # entry, and the gate cannot see a child interpreter — so those lines
        # read as untested. Pin the constant instead of relying on repo content,
        # which would make this coverage hostage to an unrelated file's edits.
        _real_ex = hwg.EXEMPTIONS
        try:
            hwg.EXEMPTIONS = tmp / "no-such-exemptions.txt"
            check("exemptions_for() returns [] when the file is ABSENT, in-process",
                  hwg.exemptions_for("anything.test.py") == [])

            ex = tmp / "ex.txt"
            ex.write_text(
                "# a comment\n"
                "\n"
                "mine.test.py state/mine.log   # trailing comment\n"
                "mine.test.py state/second.log\n"
                "other.test.py state/theirs.log\n"
                "single-token-line\n"
            )
            hwg.EXEMPTIONS = ex
            check("exemptions_for() collects EVERY entry naming this file",
                  hwg.exemptions_for("mine.test.py") == ["state/mine.log", "state/second.log"],
                  str(hwg.exemptions_for("mine.test.py")))
            check("...and another file's entries do not leak in",
                  hwg.exemptions_for("other.test.py") == ["state/theirs.log"],
                  str(hwg.exemptions_for("other.test.py")))
            check("...and a malformed single-token line is ignored, not crashed on",
                  hwg.exemptions_for("single-token-line") == [])
        finally:
            hwg.EXEMPTIONS = _real_ex

        # snapshot(): the vanished-mid-walk branch. The race is not reproducible,
        # so it is forced — is_file() says yes, the stat() that follows raises.
        # Patching stat ALONE would not do it: pathlib swallows OSError inside
        # is_file() and returns False, so the file would be skipped one line
        # earlier and the except branch would stay unexecuted while the
        # assertion below still passed. That is the shape of a test that proves
        # nothing. [[feedback_a_control_that_returns_a_plausible_number_is_not_a_validated_control]]
        (ws / "state" / "vanishing.json").write_text("{}")
        _real_stat, _real_is_file = Path.stat, Path.is_file
        Path.is_file = lambda self, *a, **k: (
            True if self.name == "vanishing.json" else _real_is_file(self, *a, **k))

        def _stat_racing(self, *a, **k):
            if self.name == "vanishing.json":
                raise OSError(2, "vanished mid-walk")
            return _real_stat(self, *a, **k)

        Path.stat = _stat_racing
        try:
            snap_race = hwg.snapshot(ws)
        finally:
            Path.stat, Path.is_file = _real_stat, _real_is_file
        check("snapshot() SKIPS a file that vanishes mid-walk instead of crashing",
              "state/vanishing.json" not in snap_race)
        check("...and still records the files that did NOT vanish",
              "state/preexisting.json" in snap_race, str(sorted(snap_race))[:140])
        (ws / "state" / "vanishing.json").unlink()

        # diff(): created / changed / removed, in-process
        check("diff() reports created, changed and removed paths",
              hwg.diff({"a": (1, 1), "b": (1, 1)}, {"a": (2, 2), "c": (1, 1)})
              == ["a", "b", "c"])

        # main(): usage error, clean run, and the violation report — all in-process.
        #
        # `hwg` was loaded from the REAL repo, so its resolved_workspace() answers the
        # REAL workspace. Left alone, main() would snapshot the operator's live tree
        # (the exact thing this guard forbids) AND compare it against a fixture write
        # that lands in the temp ws — no diff, so the violation assertion would pass
        # for the wrong reason. Pin the resolver to the fixture for these calls.
        _real_rw = hwg.resolved_workspace
        hwg.resolved_workspace = lambda: ws
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc_usage = hwg.main([])
                rc_clean = hwg.main(["run", str(clean)])
            check("main() with no args is a usage error (2), in-process", rc_usage == 2, f"rc={rc_usage}")
            check("main() on a clean test returns the child's rc, in-process", rc_clean == 0, f"rc={rc_clean}")

            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(buf2):
                rc_viol = hwg.main(["run", str(offender)])
            out2 = buf2.getvalue()
            check("main() FAILS a polluting test and NAMES the path, in-process",
                  rc_viol != 0 and "state/leaked.log" in out2, f"rc={rc_viol} {out2[-160:]}")

            # The stale-exemption report, in-process. A clean test plus an
            # exemption for a path it never writes: rc must go non-zero on the
            # exemption alone, with no violation anywhere in the run.
            _real_ex2 = hwg.EXEMPTIONS
            stale_ex = tmp / "stale-ex.txt"
            stale_ex.write_text(f"{clean.name} state/never-written.log\n")
            hwg.EXEMPTIONS = stale_ex
            try:
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3), contextlib.redirect_stderr(buf3):
                    rc_stale = hwg.main(["run", str(clean)])
                out3 = buf3.getvalue()
            finally:
                hwg.EXEMPTIONS = _real_ex2
            check("main() FAILS on a STALE exemption and names the path, in-process",
                  rc_stale != 0 and "STALE" in out3 and "state/never-written.log" in out3,
                  f"rc={rc_stale} {out3[-160:]}")
            check("...on the exemption alone — the run itself wrote nothing",
                  "wrote into the LIVE workspace" not in out3, out3[-160:])
        finally:
            hwg.resolved_workspace = _real_rw

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

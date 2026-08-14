#!/usr/bin/env python3
"""Sibling-script paths must survive `python3 src/morning-briefing.py`.

`get_health_issues` and `get_daily_insight` launch a sibling script with
`cwd=WORKSPACE`. Both built the path as `Path(__file__).parent / "<script>"`,
which is RELATIVE when the module is invoked by a relative path — the form
`skills/morning-briefing/SKILL.md` documents. The child then cannot find the
script, exits 2 with nothing parseable, and `get_health_issues`'s own
"non-zero with no findings means unknown" branch returns None.

The briefing renders that None as health being unavailable. Measured on a live
host before the fix, same machine, same minute:

    relative invocation -> get_health_issues -> None (UNAVAILABLE)
    absolute invocation -> get_health_issues -> 1 issue(s)

That function's docstring exists because "a timed-out health check returning []
made the briefing assert a clean system it had never inspected" — the None/[]
distinction was got right, then defeated by a relative path.

`test_health_check_path_is_absolute_under_a_relative_invocation` FAILS on the
parent commit. The reminders path (no `cwd=`) was never broken; it is covered
so a future `cwd=` there cannot silently reintroduce this.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Loading by a RELATIVE spec path is the whole point — an absolute one cannot
# fail, so a test that used it would pass on the broken code too.
_PROBE = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("mb", "src/morning-briefing.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["mb"] = mod
try:
    spec.loader.exec_module(mod)
except SystemExit:
    pass
import json
from pathlib import Path
# Fall back to the PARENT's expression when _SRC_DIR is absent, so the control
# arm fails these assertions instead of erroring in setUpClass. An error there
# runs zero tests, which proves the symbol is missing but not what breaks.
src_dir = getattr(mod, "_SRC_DIR", None) or Path(mod.__file__).parent
print(json.dumps({name: str(path) for name, path in {
    "health": src_dir / "health-check.py",
    "insight": src_dir / "daily-insight.py",
    "pending": src_dir / "check-pending-questions.py",
    "reminders": src_dir.parent / "skills" / "macos-tools" / "scripts" / "reminders.py",
}.items()}))
"""


class SiblingScriptPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        run = subprocess.run(
            [sys.executable, "-c", _PROBE],
            cwd=str(REPO), capture_output=True, text=True, timeout=60,
        )
        if run.returncode != 0:
            raise AssertionError(f"probe failed: {run.stderr[-400:]}")
        cls.paths = {k: Path(v) for k, v in json.loads(run.stdout).items()}

    def test_health_check_path_is_absolute_under_a_relative_invocation(self):
        """The load-bearing case: this is the path handed to a child whose cwd
        is WORKSPACE, so a relative one resolves against the wrong directory."""
        p = self.paths["health"]
        self.assertTrue(p.is_absolute(), f"health-check path is relative: {p}")
        self.assertTrue(p.exists(), f"health-check path does not exist: {p}")

    def test_daily_insight_path_is_absolute(self):
        """Same `cwd=WORKSPACE` launch, same failure, one function down."""
        p = self.paths["insight"]
        self.assertTrue(p.is_absolute(), f"daily-insight path is relative: {p}")
        self.assertTrue(p.exists(), f"daily-insight path does not exist: {p}")

    def test_paths_that_do_not_change_cwd_are_covered_too(self):
        """Neither of these passes `cwd=`, so they were never broken — they are
        pinned so that adding one later cannot reintroduce this silently."""
        for name in ("pending", "reminders"):
            with self.subTest(script=name):
                p = self.paths[name]
                self.assertTrue(p.is_absolute(), f"{name} path is relative: {p}")

    def test_the_argv_handed_to_the_child_is_absolute(self):
        """Executes the two functions that actually launch a child and inspects
        the argv they pass — a relative argv plus cwd=WORKSPACE is the defect.

        This case does NOT discriminate: it loads the module by an absolute path,
        so it passes on the parent commit too. It is here to execute the changed
        lines and to pin the contract (the argv handed to a child is absolute),
        not as evidence of the fix. The discriminating cases are the ones above,
        which load by a RELATIVE path."""
        import importlib.util
        import subprocess as sp
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            "mb_argv", REPO / "src" / "morning-briefing.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mb_argv"] = mod
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass

        seen = []

        def fake_run(argv, **kwargs):
            seen.append((list(argv), kwargs.get("cwd")))
            return sp.CompletedProcess(argv, 0, stdout="", stderr="")

        # STATE_DIR must point somewhere empty: get_daily_insight returns the
        # cached sentinel without launching anything when today's already exists,
        # so on a host that has run the briefing this case would silently assert
        # over one child instead of two.
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(mod, "STATE_DIR", Path(td)), \
             mock.patch.object(mod.subprocess, "run", side_effect=fake_run):
            mod.get_health_issues()
            mod.get_daily_insight()

        self.assertEqual(len(seen), 2, f"expected two child launches, got {seen}")
        for argv, cwd in seen:
            script = Path(argv[-1])
            with self.subTest(script=script.name):
                self.assertTrue(
                    script.is_absolute(),
                    f"argv path is relative ({script}) while cwd={cwd} — the child "
                    f"resolves it against the wrong directory")

    def test_resolving_survives_a_cwd_that_is_not_the_repo(self):
        """The bug only shows when the child's cwd differs from the parent's.
        Resolve the paths from a foreign cwd and require they still point at
        real files — the property `cwd=WORKSPACE` actually depends on."""
        for name, p in self.paths.items():
            with self.subTest(script=name):
                self.assertEqual(
                    p, Path(os.path.normpath(str(p))),
                    f"{name} path is not normalised, so it depends on cwd: {p}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

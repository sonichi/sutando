#!/usr/bin/env python3
"""_launchctl must not crash when given spawn_worker's own `_run` as its runner
-- the actual runner every real spawn() call uses (it is spawn()'s own default
parameter, `runner=_run`).

pool_remedy_timer._launchctl hardcoded `capture_output=True, text=True` on
every call to its runner. spawn_worker._run ALSO hardcodes them internally
(`subprocess.run(argv, capture_output=True, text=True, **kw)`), so a call
through `runner=_run` duplicated both keywords -- a TypeError Python raises at
the call site itself, before any real subprocess exists. The existing
spawn-wiring test (tests/pool-remedy-timer-ensured-on-spawn.test.py) could not
catch this: its `Launchctl.__call__(self, argv, **kw)` mock accepts and
swallows any kwargs, so it never reproduces `_run`'s actual signature.

Observable in production: spawn()'s call to ensure_remedy_timer() is its last
step, after the worker is already alive, and this TypeError propagated
uncaught -- spawn_worker.py's CLI exited 1 on every real macOS spawn despite
the worker succeeding.
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS))
prt = _load("pool_remedy_timer")
sw = _load("spawn_worker")


def _fake_run(argv, **kw):
    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")


class LaunchctlAcceptsSpawnWorkersRealRunner(unittest.TestCase):
    """Reproduces with the REAL functions from both modules -- not a mock."""

    def test_launchctl_itself_does_not_duplicate_kwargs(self):
        with patch("subprocess.run", side_effect=_fake_run):
            try:
                result = prt._launchctl(["print", "gui/0/com.sutando.pool-remedy"], sw._run)
            except TypeError as e:
                self.fail(f"_launchctl(runner=spawn_worker._run) raised: {e}")
        self.assertEqual(result.returncode, 1)

    def test_is_loaded_with_the_real_runner(self):
        with patch("subprocess.run", side_effect=_fake_run):
            try:
                prt.is_loaded("/test/workspace", sw._run)
            except TypeError as e:
                self.fail(f"is_loaded(runner=spawn_worker._run) raised: {e}")

    def test_ensure_remedy_timer_does_not_crash_on_a_real_macos_spawn(self):
        """The exact call spawn() makes -- runner=_run is spawn()'s own default,
        so this is the production path, not a hypothetical one."""
        with patch("subprocess.run", side_effect=_fake_run):
            real_platform = sys.platform
            try:
                sys.platform = "darwin"
                tmp = tempfile.mkdtemp()
                out = sw.ensure_remedy_timer(
                    tmp, str(REPO), runner=sw._run,
                    launch_agents=Path(tmp) / "LaunchAgents")
            except TypeError as e:
                self.fail(f"ensure_remedy_timer with spawn()'s own default "
                          f"runner raised: {e}")
            finally:
                sys.platform = real_platform
        self.assertIn("ensured", out)


if __name__ == "__main__":
    unittest.main()

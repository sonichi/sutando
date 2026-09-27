#!/usr/bin/env python3
"""`doctor` must report missing dependencies as its OWN first step.

doctor exists to answer "which step of a first connection fails", and the step
most likely to fail on a fresh host is the first one: the deps are not
installed. But room_collab_client exits at import time when they are absent, so
any import of it reached before the deps check pre-empts the report — the agent
gets a bare exit instead of the step, and rc 1 instead of doctor's rc 2.

This is a process-level property of import ORDER, so it is exercised by running
the CLI as a subprocess with the deps hidden, not by importing anything here.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "skills" / "room-collab" / "scripts" / "room_collab.py"

FAILS = []

BLOCKER = """
import sys
_BLOCK = {"websockets", "pycrdt"}
class _Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in _BLOCK:
            raise ModuleNotFoundError("No module named %r" % name.split(".")[0])
        return None
sys.meta_path.insert(0, _Blocker())
"""


def run_doctor(pythonpath: str | None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run([sys.executable, str(CLI), "doctor", "!room:server"],
                          capture_output=True, text=True, cwd=str(REPO), env=env)


def check(name, fn):
    try:
        fn()
    except AssertionError as e:
        FAILS.append(f"{name}: {e}")
    except Exception as e:  # noqa: BLE001
        FAILS.append(f"{name}: unexpected {type(e).__name__}: {e}")


def test_missing_deps_are_reported_as_doctors_own_step():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "sitecustomize.py").write_text(BLOCKER)
        blocked = run_doctor(d)
        present = run_doctor(None)

    # Control: the blocker is what changes the outcome. Without it the deps
    # step passes, so a pass below cannot come from deps being absent anyway.
    assert "ok" in present.stdout and "deps" in present.stdout, (
        f"control: deps step did not pass with deps installed:\n{present.stdout}{present.stderr}")

    assert "deps" in blocked.stdout, (
        "doctor printed no deps step with the deps hidden — an import reached "
        f"the client before the check:\nstdout={blocked.stdout!r}\nstderr={blocked.stderr!r}")
    assert "FAIL" in blocked.stdout, (
        f"deps step was not reported as FAIL:\n{blocked.stdout}")
    assert blocked.returncode == 2, (
        f"expected doctor's rc 2, got {blocked.returncode}; "
        f"stdout={blocked.stdout!r} stderr={blocked.stderr!r}")
    assert "room-collab doctor:" in blocked.stdout, (
        f"doctor's own header never printed:\n{blocked.stdout!r}")


check("missing deps are reported as doctor's own step",
      test_missing_deps_are_reported_as_doctors_own_step)

if FAILS:
    print("FAIL")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("ok - doctor reports missing deps as its own first step")

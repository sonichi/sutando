#!/usr/bin/env python3
"""A host that spawns a worker gets the remedy timer, without anyone running
`pool_remedy_timer.py install` by hand.

The timer is what resumes a dead worker unattended. Installing it was a manual
step with no caller anywhere in the tree, so a host could run workers that
nothing would ever recover — measured on a peer host: three dead workers,
`installed: False`, and a remedy log that had never been written.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS))
sw = _load("spawn_worker")

fails = 0


def ok(m):
    print(f"  ok  {m}")


def fail(m):
    global fails
    fails += 1
    print(f"FAIL: {m}")


class Launchctl:
    """Records argv instead of reaching the live launchd domain. `loaded` is
    what `launchctl print` answers, so a test can present an installed-but-
    unloaded job as well as a healthy one."""

    def __init__(self, loaded=False, bootstrap_rc=0):
        self.loaded, self.bootstrap_rc, self.argv = loaded, bootstrap_rc, []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        sub = argv[1] if len(argv) > 1 else ""
        rc = 0
        if sub == "print":
            rc = 0 if self.loaded else 1
        elif sub == "bootstrap":
            rc = self.bootstrap_rc
            if rc == 0:
                self.loaded = True
        elif sub == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    def ran(self, sub):
        return [a for a in self.argv if len(a) > 1 and a[1] == sub]


HAVE_HELPER = hasattr(sw, "ensure_remedy_timer")


def case(name, *, loaded, bootstrap_rc=0, platform="darwin"):
    """Run ensure_remedy_timer against a throwaway LaunchAgents dir.

    A checkout without the helper answers with the absence rather than raising,
    so running this file against an unfixed script reports failing assertions
    instead of a traceback — the control has to be readable to be evidence."""
    if not HAVE_HELPER:
        return {"ensured": None, "why": "spawn_worker has no ensure_remedy_timer"}, Launchctl(), Path(tempfile.mkdtemp())
    tmp = tempfile.mkdtemp()
    la = Path(tmp) / "LaunchAgents"
    la.mkdir()
    if loaded:  # an already-installed host has the plist on disk too
        lc0 = Launchctl(loaded=False)
        sw.prt.install(tmp, str(REPO), launch_agents=la, runner=lc0, sleep=lambda *_: None)
    lc = Launchctl(loaded=loaded, bootstrap_rc=bootstrap_rc)
    real = sys.platform
    try:
        sys.platform = platform
        out = sw.ensure_remedy_timer(tmp, str(REPO), runner=lc, launch_agents=la)
    finally:
        sys.platform = real
    return out, lc, la


# --- the defect: a host with no timer gets one when it spawns a worker -------
out, lc, la = case("fresh", loaded=False)
if out.get("ensured") is True:
    ok("a host with no timer installs it")
else:
    fail(f"no timer was installed on a fresh host: {out}")
if (la / "com.sutando.pool-remedy.plist").exists():
    ok("the plist is on disk")
else:
    fail("no plist written")
if lc.ran("bootstrap"):
    ok("the job was bootstrapped into launchd")
else:
    fail(f"never bootstrapped: {lc.argv}")

# --- already installed: do NOT re-install ------------------------------------
# pool_remedy calls spawn() from INSIDE this launchd job; re-installing would
# bootout the job that is running.
out, lc, _ = case("installed", loaded=True)
if out.get("ensured") is False and out.get("why") == "already installed":
    ok("an installed+loaded timer is left alone")
else:
    fail(f"re-installed over a healthy timer: {out}")
if not lc.ran("bootout"):
    ok("the running job was not booted out")
else:
    fail(f"booted out the job it may be running inside: {lc.argv}")

# --- a failing install must not raise ----------------------------------------
out, _, _ = case("broken", loaded=False, bootstrap_rc=1)
if out.get("ensured") is False and "RuntimeError" in str(out.get("why")):
    ok("a bootstrap failure is reported, not raised")
else:
    fail(f"bootstrap failure not reported as a value: {out}")

# --- non-darwin is a no-op, not a crash --------------------------------------
out, lc, _ = case("linux", loaded=False, platform="linux")
if out.get("ensured") is False and "macOS" in str(out.get("why")):
    ok("launchd is skipped off macOS")
else:
    fail(f"tried to use launchd off macOS: {out}")
if not lc.argv:
    ok("no launchctl call off macOS")
else:
    fail(f"called launchctl off macOS: {lc.argv}")

# --- the wiring: spawn() must actually call it -------------------------------
# The helper existing is not the fix; a spawn that never calls it leaves the
# host exactly as broken.
src = (SCRIPTS / "spawn_worker.py").read_text()
body = src[src.index("def spawn(workspace, repo"):src.index("def main(")]
if "ensure_remedy_timer(" in body:
    ok("spawn() calls ensure_remedy_timer")
else:
    fail("spawn() never calls ensure_remedy_timer — the host stays unrecovered")
if '"remedy_timer"' in body:
    ok("the spawn result reports what happened to the timer")
else:
    fail("spawn() does not report the timer outcome")

if fails:
    print(f"pool-remedy-timer-ensured-on-spawn: {fails} failure(s)")
    sys.exit(1)
print("pool-remedy-timer-ensured-on-spawn: all ok")

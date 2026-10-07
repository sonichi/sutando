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
import plistlib
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
# Exercise macOS timer behavior on every CI host with a fake launchctl and temp LaunchAgents.
sys.platform = "darwin"

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

    def __init__(self, loaded=(), bootstrap_rc=0):
        self.loaded, self.bootstrap_rc, self.argv = set(loaded), bootstrap_rc, []

    def __call__(self, argv, **kw):
        self.argv.append(list(argv))
        sub = argv[1] if len(argv) > 1 else ""
        rc = 0
        if sub == "print":
            rc = 0 if argv[2] in self.loaded else 1
        elif sub == "bootstrap":
            rc = self.bootstrap_rc
            if rc == 0:
                with open(argv[3], "rb") as fh:
                    label = plistlib.load(fh)["Label"]
                self.loaded.add(f"gui/{os.getuid()}/{label}")
        elif sub == "bootout":
            self.loaded.discard(argv[2])
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
    loaded_labels = set()
    if loaded:  # an already-installed host has the plist on disk too
        lc0 = Launchctl()
        sw.prt.install(tmp, str(REPO), launch_agents=la, runner=lc0, sleep=lambda *_: None)
        loaded_labels = lc0.loaded
    lc = Launchctl(loaded=loaded_labels, bootstrap_rc=bootstrap_rc)
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
if sw.prt.plist_path(la.parent, la).exists():
    ok("the plist is on disk")
else:
    fail("no plist written")
if lc.ran("bootstrap"):
    ok("the job was bootstrapped into launchd")
else:
    fail(f"never bootstrapped: {lc.argv}")

# --- already installed: do NOT re-install ------------------------------------
# pool_remedy calls spawn() from INSIDE this job; re-installing would bootout it.
out, lc, la = case("installed", loaded=True)
if out.get("ensured") is False and out.get("why") == "already installed":
    ok("an installed+loaded timer is left alone")
else:
    fail(f"re-installed over a healthy timer: {out}")
if not lc.ran("bootout"):
    ok("the running job was not booted out")
else:
    fail(f"booted out the job it may be running inside: {lc.argv}")

# --- two loaded jobs for one workspace require explicit cleanup -------------
job = sw.prt.render(la.parent, REPO)
job["Label"] = sw.prt.LABEL
legacy_path = sw.prt.legacy_plist_path(la)
with open(legacy_path, "wb") as fh:
    plistlib.dump(job, fh)
lc(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(legacy_path)])
lc.argv.clear()
out = sw.ensure_remedy_timer(la.parent, REPO, runner=lc, launch_agents=la)
if out.get("conflict") is True and not lc.ran("bootout"):
    ok("automatic ensure reports duplicate legacy and workspace timers")
else:
    fail(f"duplicate sweepers were silently accepted: {out}, {lc.argv}")

# --- different workspaces keep independent timers ---------------------------
tmp = Path(tempfile.mkdtemp())
la = tmp / "LaunchAgents"
la.mkdir()
old_ws, new_ws = tmp / "old-workspace", tmp / "new-workspace"
lc = Launchctl()
sw.prt.install(old_ws, str(REPO), launch_agents=la, runner=lc, sleep=lambda *_: None)
lc.argv.clear()
real = sys.platform
try:
    sys.platform = "darwin"
    out = sw.ensure_remedy_timer(new_ws, str(REPO),
                                 runner=lc, launch_agents=la)
finally:
    sys.platform = real
if out.get("ensured") is True and not lc.ran("bootout") and lc.ran("bootstrap"):
    ok("a second workspace gets its own timer without booting out the first")
else:
    fail(f"second workspace displaced the first timer: {out}, {lc.argv}")
if (sw.prt.status(old_ws, launch_agents=la, runner=lc).get("loaded")
        and sw.prt.status(new_ws, launch_agents=la, runner=lc).get("workspace")
        == str(new_ws.resolve())):
    ok("both workspace timers remain loaded with their own targets")
else:
    fail("one workspace timer lost its target")

# --- a dev checkout cannot take over a timer for the same workspace ----------
lc.argv.clear()
out = sw.ensure_remedy_timer(old_ws, tmp / "different-checkout",
                             runner=lc, launch_agents=la)
if out.get("conflict") is True and not lc.ran("bootout") and not lc.ran("bootstrap"):
    ok("automatic ensure refuses a different repo for the same workspace")
else:
    fail(f"a different repo rebound an existing timer: {out}, {lc.argv}")

# --- the executable path matters even when --repo still names this checkout --
path = sw.prt.plist_path(old_ws, la)
with open(path, "rb") as fh:
    job = plistlib.load(fh)
job["ProgramArguments"][1] = "/scratch/checkout/pool_remedy.py"
with open(path, "wb") as fh:
    plistlib.dump(job, fh)
lc.argv.clear()
out = sw.ensure_remedy_timer(old_ws, REPO, runner=lc, launch_agents=la)
if out.get("conflict") is True and not lc.ran("bootout") and not lc.ran("bootstrap"):
    ok("automatic ensure refuses a timer running code from another checkout")
else:
    fail(f"a foreign executable was silently accepted or replaced: {out}, {lc.argv}")

# --- the legacy singleton in a live app workspace must not be seized --------
legacy_ws = tmp / "legacy-workspace"
legacy = sw.prt.render(legacy_ws, tmp / "dev-checkout")
legacy["Label"] = sw.prt.LABEL
legacy_path = sw.prt.legacy_plist_path(la)
with open(legacy_path, "wb") as fh:
    plistlib.dump(legacy, fh)
lc(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(legacy_path)])
lc.argv.clear()
out = sw.ensure_remedy_timer(legacy_ws, REPO, runner=lc, launch_agents=la)
if out.get("conflict") is True and not lc.ran("bootout") and not lc.ran("bootstrap"):
    ok("automatic ensure refuses to seize a same-workspace legacy timer from another repo")
else:
    fail(f"a live legacy timer was displaced: {out}, {lc.argv}")

# --- a matching legacy job may be executing this very spawn ------------------
# An automatic install used to bootout the loaded singleton during its sweep.
matching_ws = tmp / "matching-legacy-workspace"
matching = sw.prt.render(matching_ws, REPO)
matching["Label"] = sw.prt.LABEL
with open(legacy_path, "wb") as fh:
    plistlib.dump(matching, fh)
lc.loaded = {sw.prt.legacy_service_target()}
lc.argv.clear()
out = sw.ensure_remedy_timer(matching_ws, REPO, runner=lc, launch_agents=la)
if (out.get("why") == "already installed" and out.get("legacy_retained") is True
        and not lc.ran("bootout") and not lc.ran("bootstrap")
        and legacy_path.exists() and not sw.prt.plist_path(matching_ws, la).exists()):
    ok("auto-ensure leaves a loaded matching legacy job running through its sweep")
else:
    fail(f"auto-ensure displaced its own loaded legacy job: {out}, {lc.argv}")

# An unloaded legacy plist cannot provide recovery, and leaving it alongside
# a new job would allow two sweepers after login. Require explicit migration.
lc.loaded.clear()
lc.argv.clear()
out = sw.ensure_remedy_timer(matching_ws, REPO, runner=lc, launch_agents=la)
if (out.get("conflict") is True and "explicit install" in out.get("why", "")
        and not lc.ran("bootout") and not lc.ran("bootstrap")
        and legacy_path.exists() and not sw.prt.plist_path(matching_ws, la).exists()):
    ok("an unloaded matching legacy plist requires explicit migration")
else:
    fail(f"auto-ensure migrated an unloaded legacy plist: {out}, {lc.argv}")

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
# The helper existing is not the fix; an uncalled one leaves the host broken.
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

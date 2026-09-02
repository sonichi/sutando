#!/usr/bin/env python3
"""The secret-scanner probe must measure the interpreter a bridge RUNS on.

THE DEFECT (live on this host 2026-08-27 -> 2026-08-31, issue #3580). The probe
asked `_bridge_interpreter(name)` — "which interpreter WOULD launch this" — and
reported `ok: detect-secrets present in all 3 bridge interpreter(s)` while the
ag2.space gateway bridge ran on the app's bundled python, which has no
`detect_secrets`. Inbound text was scanned with 17 provider detectors and the
entropy checks off, and health-check printed green over it for four days.

WHY THE OBVIOUS FIX IS A TRAP, and why this test asserts on the interpreter
rather than on the bridge list: simply adding the gateway to the population does
NOT work. `_bridge_interpreter` returns `sys.executable` for a bridge with no
required import, and that interpreter HAS the module — so the probe would print
`present in all 4` and look MORE authoritative while the gap persisted. The
population was never the bug; the question was.

THE CONTROLS ARE THE POINT. "Reports warn" is free for any function that always
warns, so each case below pins a polarity the naive implementation gets wrong:
a spaced interpreter path (shlex splits it mid-path), a `src/` prefixed script
arg (cutting at the bare name leaves a trailing slash), and a shell whose own
argv contains the script name — the self-match that `_proc_argv`'s docstring in
this same module already warns about.

ROUND 2 (qingyun-wu on #3597). The first implementation sliced argv at the last
space before the script name. When the SCRIPT path also contains spaces that
boundary falls inside the script path, so the helper returned None, the caller
fell back to `_bridge_interpreter()`, and the exact would-launch false green this
PR removes came straight back. The executable is now read from the matched PID
via `ps -o comm=` — one field, so a spaced path survives it and no argv boundary
has to be recovered. The spaced-both case and a composed warn/ok polarity pair
are pinned below.
"""
import importlib.util
import os
import pathlib
import sys
import tempfile
import types

# health-check resolves workspace/channel config at import, so isolate before
# exec_module or this reads the developer's real per-user config.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-secret-scanner-")

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("hc", SRC)
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- resolution -------------------------------------------------------------
# `exe_of` stands in for `ps -o comm=` so these synthetic PIDs resolve.
SPACED_INTERP = ("  76550 76531 /Users/x/App Support/rt/python/bin/python3"
                 " src/remote-gateway-bridge.py\n")
SPACED_BOTH = ("  76550 76531 /Users/x/App Support/rt/python/bin/python3"
               " /Users/x/My Sutando/src/remote-gateway-bridge.py\n")
BUNDLED = "/Users/x/App Support/rt/python/bin/python3"
exe_ok = lambda pid: BUNDLED if pid == "76550" else None

check("spaced interpreter path", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", SPACED_INTERP, exe_ok), [BUNDLED])

# The round-1 regression: slicing argv at the last space before the script name
# lands INSIDE a spaced script path, returning None -> false-green fallback.
check("spaces in BOTH interpreter and script paths", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", SPACED_BOTH, exe_ok), [BUNDLED])

# The probing shell's own argv contains the script name; its executable is not python.
SELF = "  999 1 /bin/zsh -c grep remote-gateway-bridge.py somewhere\n"
check("shell self-match is not a bridge", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", SELF, lambda pid: "/bin/zsh"), [])

check("no ps output -> None",
      hc._live_bridge_interpreters("remote-gateway-bridge.py", "", exe_ok), [])
check("absent bridge -> None",
      hc._live_bridge_interpreters("not-running-bridge.py", SPACED_BOTH, exe_ok), [])
check("executable unresolvable -> None", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", SPACED_BOTH, lambda pid: None), [])

# _proc_executable against the REAL ps: everything above injects exe_of, so
# without this nothing exercises the helper that reads the process table.
_self = hc._proc_executable(os.getpid())
if not _self or "python" not in os.path.basename(_self).lower():
    failures.append(f"_proc_executable on our own live PID should name a python, got {_self!r}")
check("framework Python (capital P) is still a python", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", SPACED_BOTH,
    lambda pid: "/L/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"),
    ["/L/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python"])

# startup-runtime.sh launches one gateway per AG2_REMOTE_TOKEN_*, so a scalar
# both under-collects and makes the answer depend on ps row order.
TWO_A = ("  10 1 /healthy/python3 /x/src/remote-gateway-bridge.py\n"
         "  20 1 /degraded/python3 /y/src/remote-gateway-bridge.py\n")
TWO_B = ("  20 1 /degraded/python3 /y/src/remote-gateway-bridge.py\n"
         "  10 1 /healthy/python3 /x/src/remote-gateway-bridge.py\n")
two = lambda pid: {"10": "/healthy/python3", "20": "/degraded/python3"}.get(pid)
_a = hc._live_bridge_interpreters("remote-gateway-bridge.py", TWO_A, two)
_b = hc._live_bridge_interpreters("remote-gateway-bridge.py", TWO_B, two)
check("two live gateways: BOTH collected", _a, ["/degraded/python3", "/healthy/python3"])
check("selection is ps-row-order invariant", _a, _b)

# `script in argv` over-matched: a -c payload that merely prints the name is not
# a launch. The script must appear as its own argv token.
DECOY = "  900 1 /decoy/python3 -c print('remote-gateway-bridge.py')\n"
check("a -c decoy naming the script is not a bridge", hc._live_bridge_interpreters(
    "remote-gateway-bridge.py", DECOY, lambda pid: "/decoy/python3"), [])
check("_proc_executable on an impossible PID -> None", hc._proc_executable(2**31 - 1), None)

_platform_executable = hc._platform_process_executable
hc._platform_process_executable = lambda pid: None
check("_proc_executable when ps cannot run -> None", hc._proc_executable(os.getpid()), None)
hc._platform_process_executable = _platform_executable

# The gateway bridge must be IN the scanned population at all.
if "remote-gateway-bridge" not in hc._VAULT_SCANNER_BRIDGES:
    failures.append("gateway bridge missing from _VAULT_SCANNER_BRIDGES")
if set(hc._VAULT_SCANNER_SCRIPTS) != set(hc._VAULT_SCANNER_BRIDGES):
    failures.append("_VAULT_SCANNER_SCRIPTS and _VAULT_SCANNER_BRIDGES disagree")

# The COMPOSED check, driven through _ps_snapshot + the parser rather than a stub
# of the helper, so a parser regression fails here too.
_snap, _exe = hc._ps_snapshot, hc._proc_executable
hc._ps_snapshot = lambda: SPACED_BOTH
hc._proc_executable = lambda pid: BUNDLED if pid == "76550" else None

r = hc.check_secret_scanner_mode()
if r["status"] != "warn":
    failures.append(f"spaced-both live interpreter must WARN, got {r['status']}")
if BUNDLED not in r["detail"]:
    failures.append("warn must NAME the live interpreter it probed, not a would-launch one")

# Healthy interpreter -> ok, or the warn above is free. Health is STUBBED, not
# borrowed: keying it on sys.executable failed where detect-secrets is absent.
_run = hc.subprocess.run
hc._proc_executable = lambda pid: sys.executable if pid == "76550" else None
hc.subprocess.run = lambda *a, **k: types.SimpleNamespace(
    returncode=0, stdout="", stderr="")
r = hc.check_secret_scanner_mode()
hc.subprocess.run = _run
if r["status"] != "ok":
    failures.append(f"a healthy live interpreter must be ok, got {r['status']}")

hc._ps_snapshot, hc._proc_executable = _snap, _exe

if failures:
    for f in failures:
        print(f"FAIL {f}")
    raise SystemExit(1)
print("health-check-secret-scanner-live-interpreter: all assertions passed")

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
"""
import importlib.util
import pathlib
import sys

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


# A real gateway-bridge row: interpreter path contains spaces, script carries src/.
SPACED = ("  76550 76531 /Users/x/Application Support/app/runtime/python/bin/python3"
          " src/remote-gateway-bridge.py\n")
check("spaced interpreter path resolves whole",
      hc._live_bridge_interpreter("remote-gateway-bridge.py", SPACED),
      "/Users/x/Application Support/app/runtime/python/bin/python3")

# The probing shell's own argv contains the script name; it is not a bridge.
SELF = "  999 1 /bin/zsh -c grep remote-gateway-bridge.py somewhere\n"
check("shell self-match is not a bridge",
      hc._live_bridge_interpreter("remote-gateway-bridge.py", SELF), None)

# ps unavailable must not manufacture an answer in either direction.
check("no ps output -> None",
      hc._live_bridge_interpreter("remote-gateway-bridge.py", ""), None)

# A bridge that is not running yields None so the caller can fall back.
check("absent bridge -> None",
      hc._live_bridge_interpreter("telegram-bridge.py", SPACED), None)

# The gateway bridge must be IN the scanned population at all.
if "remote-gateway-bridge" not in hc._VAULT_SCANNER_BRIDGES:
    failures.append("gateway bridge missing from _VAULT_SCANNER_BRIDGES")
if set(hc._VAULT_SCANNER_SCRIPTS) != set(hc._VAULT_SCANNER_BRIDGES):
    failures.append("_VAULT_SCANNER_SCRIPTS and _VAULT_SCANNER_BRIDGES disagree")

# Both polarities of the probe itself, so neither verdict is free.
_orig = hc._live_bridge_interpreter
hc._live_bridge_interpreter = lambda script, ps=None: sys.executable
check("all interpreters healthy -> ok", hc.check_secret_scanner_mode()["status"], "ok")

hc._live_bridge_interpreter = lambda script, ps=None: "/nonexistent/python-no-detect-secrets"
check("a degraded interpreter -> warn", hc.check_secret_scanner_mode()["status"], "warn")
hc._live_bridge_interpreter = _orig

if failures:
    for f in failures:
        print(f"FAIL {f}")
    raise SystemExit(1)
print("health-check-secret-scanner-live-interpreter: all assertions passed")

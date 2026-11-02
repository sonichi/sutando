#!/usr/bin/env python3
"""An armed pin must veto the WEDGED restart prescription too.

`check_port` returns `status="wedged"` with `detail="... restart needed"` when a
port accepts but never answers. That is a restart prescription, and it was the
one prescription that never consulted the pin: the pin was checked inside
`mark_stale_if_outdated`, which four of the seven call sites reach only when the
status is already "ok".

So a pinned process that WEDGED was still prescribed a restart -- and a restart
destroys the branch-only witness the pin exists to preserve, exactly as a
stale-code restart would.

The veto belongs at the prescription, not at the call sites: fixing the callers
would need the same patch four times and still miss the other three.
"""
import importlib.util
import json
import pathlib
import socket
import sys
import tempfile
import threading
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


spec = importlib.util.spec_from_loader("hc", loader=None)
hc = importlib.util.module_from_spec(spec)
hc.__file__ = str(REPO / "src" / "health-check.py")
sys.path.insert(0, str(REPO / "src"))
exec(compile((REPO / "src" / "health-check.py").read_text(),
             str(REPO / "src" / "health-check.py"), "exec"), hc.__dict__)

tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-wedged-pin-"))
(tmp / "state").mkdir(parents=True, exist_ok=True)
hc.WORKSPACE_DIR = tmp

SERVICE = "probe-svc"
PID, LSTART = 4242, "Mon Aug 25 12:00:00 2026"
# pgrep yields STRING pids and evaluate() keys on that; an int key
# scores "orphan" and fails for the wrong reason.
hc._proc_lstarts = lambda _pat: ([time.time()], {str(PID): LSTART})

# A port that ACCEPTS and never answers -- the definition of wedged.
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
port = srv.getsockname()[1]
srv.listen(8)
stop = threading.Event()
held = []


def accept_and_hold():
    srv.settimeout(0.3)
    while not stop.is_set():
        try:
            c, _ = srv.accept()
            held.append(c)          # never send, never close
        except Exception:
            pass


th = threading.Thread(target=accept_and_hold, daemon=True)
th.start()

try:
    pins = tmp / "state" / "process-pins.json"

    # --- CONTROL FIRST: no pin -> the prescription must still stand ---------
    pins.write_text(json.dumps({"pins": []}))
    base = hc.check_port(port, SERVICE, probe=True)
    check(base["status"] == "wedged",
          f"control: with NO pin the verdict is still wedged (got {base['status']})")
    check("restart needed" in base["detail"],
          "control: and it still prescribes a restart")

    # --- ARMED pin -> the restart must be vetoed ----------------------------
    pins.write_text(json.dumps({"pins": [{
        "service": SERVICE, "pid": PID, "lstart": LSTART,
        "reason": "witness armed in this process",
        "pinned_at": "2026-08-25T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }]}))
    armed = hc.check_port(port, SERVICE, probe=True)
    # The pin vetoes the REMEDY, never the DIAGNOSIS: `warn` is a benign
    # status, so downgrading here drops a live outage out of `issues`.
    check(armed["status"] == "wedged",
          f"the diagnosis SURVIVES the pin (got {armed['status']})")
    check(armed["status"] not in ("ok", "warn"),
          "and the status is not benign, so --quiet still exits non-zero")
    check("restart needed" not in armed["detail"],
          "the restart prescription is withdrawn")
    check("DO NOT RESTART" in armed["detail"],
          "and the pin's own reason is surfaced instead")
    check("listening but unresponsive" in armed["detail"],
          "while the observed condition is still reported, not hidden")
finally:
    stop.set()
    for c in held:
        try:
            c.close()
        except Exception:
            pass
    srv.close()

if fail:
    print("FAIL: an armed pin does not veto the wedged restart")
    sys.exit(1)
print("PASS: an armed pin vetoes the wedged restart prescription.")

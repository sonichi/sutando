#!/usr/bin/env python3
"""An armed pin must veto the RESTART ACTION, not just the restart text.

`check_port` was taught to preserve `status="wedged"` and withdraw the
"restart needed" wording when a pin is armed. That fixed the DIAGNOSIS and
left the ACTION untouched: the wedged row still enters `issues`, and main's
`--fix` dispatch selects a remedy from the row's NAME and STATUS alone. So
the UI said DO NOT RESTART while `--fix` destroyed the pinned witness.

Prose in `detail` cannot gate an action. The veto is carried structurally as
`restart_veto` and enforced once at the shared action boundary -- the top of
the fix loop, which every remedy branch passes through -- rather than in each
branch, which is the same patch N times and misses the N+1th.
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

tmp = pathlib.Path(tempfile.mkdtemp(prefix="hc-fix-veto-"))
(tmp / "state").mkdir(parents=True, exist_ok=True)
hc.WORKSPACE_DIR = tmp
pins = tmp / "state" / "process-pins.json"

PID, LSTART = 4242, "Mon Aug 25 12:00:00 2026"
hc._proc_lstarts = lambda _pat: ([time.time()], {str(PID): LSTART})

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
            held.append(c)
        except Exception:
            pass


threading.Thread(target=accept_and_hold, daemon=True).start()


def arm(service, armed):
    pins.write_text(json.dumps({"pins": [{
        "service": service, "pid": PID, "lstart": LSTART,
        "reason": "witness armed in this process",
        "pinned_at": "2026-08-25T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
    }]} if armed else {"pins": []}))


def run_fix(service, armed):
    """Drive the REAL `main --fix` dispatch over a REAL check_port row."""
    arm(service, armed)
    row = hc.check_port(port, service, probe=True)
    calls = []
    orig_fix, orig_all, orig_argv = hc.fix_launchd, hc.run_all_checks, sys.argv
    hc.fix_launchd = lambda label: (calls.append(label), "restarted")[1]
    hc.run_all_checks = lambda: [row]
    sys.argv = ["health-check.py", "--fix"]
    try:
        hc.main()
    except SystemExit:
        pass
    finally:
        hc.fix_launchd, hc.run_all_checks, sys.argv = orig_fix, orig_all, orig_argv
    return row, calls


def quiet_exit(row):
    """The real `--quiet` exit status for a checks list holding this row."""
    orig_all, orig_argv = hc.run_all_checks, sys.argv
    hc.run_all_checks = lambda: [row]
    sys.argv = ["health-check.py", "--quiet"]
    try:
        hc.main()
        return 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    finally:
        hc.run_all_checks, sys.argv = orig_all, orig_argv


try:
    # `== 1` certifies nothing unless the probe can also return 0, and `warn`
    # is excluded from `issues` -- exactly how a downgrade would flip --quiet.
    check(quiet_exit({"name": "probe", "status": "warn", "detail": "benign"}) == 0,
          "probe control: a benign row DOES exit 0, so `== 1` discriminates")

    for service in ("voice-agent", "web-client"):
        # --- CONTROL: unpinned. The restart MUST still fire, or this test
        # would pass on a build where --fix simply stopped working.
        row, calls = run_fix(service, armed=False)
        check(row["status"] == "wedged",
              f"{service}: control is wedged (got {row['status']})")
        check(not row.get("restart_veto"),
              f"{service}: control carries no veto")
        check(calls == [hc.LAUNCHD_BACKED_CHECKS[service]],
              f"{service}: control DOES restart -- {calls}")

        # --- ARMED: same row shape, restart withheld.
        row, calls = run_fix(service, armed=True)
        check(row["status"] == "wedged",
              f"{service}: armed row keeps the diagnosis (got {row['status']})")
        check(row.get("restart_veto"),
              f"{service}: armed row carries a STRUCTURED veto, not just prose")
        check(hc.is_issue(row),
              f"{service}: retains ISSUE MEMBERSHIP (a `warn` would drop out)")
        check(quiet_exit(row) == 1,
              f"{service}: and --quiet still EXITS 1 (got {quiet_exit(row)})")
        check(calls == [],
              f"{service}: armed pin BLOCKS the restart -- got {calls}")
finally:
    stop.set()
    for c in held:
        try:
            c.close()
        except Exception:
            pass
    srv.close()

# ---- the two arms that never call check_port's normal return path ----------
for armed in (False, True):
    arm("web-client", armed)
    # check_port's OUTER error arm: probe raises, so no earlier arm resolves a pin.
    err = hc.check_port(-1, "web-client", probe=True)
    check(err["status"] == "error", f"error arm reached (got {err['status']})")
    check(bool(err.get("restart_veto")) == armed,
          f"error arm veto=={armed} (got {err.get('restart_veto') is not None})")

    # run_all_checks' SYNTHESIZED down row for an unusable CLIENT_PORT: built
    # without check_port at all, so it must resolve the pin itself.
    orig_res = hc.resolve_web_client_port
    hc.resolve_web_client_port = lambda: {"error": "invalid CLIENT_PORT='x'"}
    try:
        rows = {c["name"]: c for c in hc.run_all_checks()}
    finally:
        hc.resolve_web_client_port = orig_res
    w = rows.get("web-client", {})
    check(w.get("status") == "down", f"synthesized down row reached ({w.get('status')})")
    check(bool(w.get("restart_veto")) == armed,
          f"synthesized row veto=={armed} (got {w.get('restart_veto') is not None})")

if fail:
    print("FAIL: --fix restarts a pinned process")
    sys.exit(1)
print("PASS: an armed pin vetoes the restart ACTION, not just its wording.")

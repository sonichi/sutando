#!/usr/bin/env python3
"""An OPTIONAL service's row must carry its pin, and --fix must obey it.

The optional adapter (:7843/:7844/:7845) replaced check_port's diagnosis with
`not running (optional)` and never performed final pin composition, so a down
pinned process lost its veto from the owner-facing detail. A HEALTHY optional
service was worse: check_port returns plain `ok` without evaluating pins at
all, so its pin was never read. The special screen-capture `--fix` dispatch
then called fix_screen_capture() without consulting the structured veto -- and
that fixer kills any :7845 listener before it checks anything else, so the
opposite pin inputs selected the same destructive act.

Reciprocal controls, because an assertion that only ever sees the pinned case
passes by construction: unpinned-down MUST still dispatch the fixer, and
unpinned-healthy MUST stay plain `ok` with no veto.

Run: python3 tests/health-check-optional-service-pin-composes.test.py
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-opt-pin-")
_ccd = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "slack"
_ccd.mkdir(parents=True, exist_ok=True)
(_ccd / "access.json").write_text("{}")

REPO = Path(__file__).resolve().parents[1]
PID = "616161"
LSTART = "Mon Aug 25 00:00:00 2026"

fail = 0


def check(cond, label):
    global fail
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        fail = 1


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _load():
    spec = importlib.util.spec_from_file_location("hc_opt_pin", REPO / "src/health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(pinned: bool, healthy: bool):
    """The real check_port -> run_all_checks() screen-capture row."""
    sys.path.insert(0, str(REPO / "src"))
    import process_pins

    sock, stop = None, threading.Event()
    if healthy:
        # check_port(probe=True) requires RESPONSE BYTES -- an accept-only
        # socket is classified `wedged`, which is a different branch entirely.
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        test_port = sock.getsockname()[1]

        def serve():
            sock.settimeout(0.3)
            while not stop.is_set():
                try:
                    conn, _ = sock.accept()
                except Exception:
                    continue
                try:
                    conn.recv(4096)
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n"
                                 b"Connection: close\r\n\r\n")
                except Exception:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=serve, daemon=True).start()
    else:
        test_port = _free_port()  # bound then released: genuinely closed

    try:
        with tempfile.TemporaryDirectory() as td:
            ws, repo = Path(td) / "ws", Path(td) / "repo"
            (ws / "state").mkdir(parents=True)
            (repo / "src").mkdir(parents=True)
            (repo / ".env").write_text("")

            mod = _load()
            mod.WORKSPACE_DIR = ws
            mod.REPO_DIR = repo
            if pinned:
                process_pins.arm_pin(ws / "state" / "process-pins.json",
                                     "screen-capture", PID, LSTART,
                                     "branch-only witness in flight",
                                     "2099-01-01T00:00:00Z")

            mod._resolve_dotenv = lambda: repo / ".env"
            mod.mark_stale_if_outdated = lambda *a, **k: None
            # Only this service's process seam moves; _pin_verdicts filters
            # every other service's pins out by name.
            mod._proc_lstarts = lambda pat: ([0.0], {PID: LSTART})

            real_port = mod.check_port

            def port(p, name, *a, **k):
                # Redirect :7845 onto the real socket/closed port under test so
                # check_port itself stays REAL -- the defect lives downstream.
                if p == 7845:
                    return real_port(test_port, name, *a, **k)
                return real_port(p, name, *a, **k)

            mod.check_port = port
            checks = mod.run_all_checks()
            row = next((c for c in checks if c.get("name") == "screen-capture"), None)
            assert row is not None, "screen-capture produced no check row"
            return mod, row
    finally:
        stop.set()
        if sock is not None:
            sock.close()


def _fix_dispatch(mod, row):
    """Drive the REAL `main --fix` dispatch; report whether the fixer fired."""
    calls = []
    orig_fix, orig_all, orig_argv = (mod.fix_screen_capture, mod.run_all_checks, sys.argv)
    mod.fix_screen_capture = lambda: (calls.append("screen-capture"), "restarted")[1]
    mod.run_all_checks = lambda: [row]
    mod.fix_down_bridges = lambda checks: []
    sys.argv = ["health-check.py", "--fix"]
    try:
        mod.main()
    except SystemExit:
        pass
    finally:
        mod.fix_screen_capture, mod.run_all_checks, sys.argv = orig_fix, orig_all, orig_argv
    return calls


# --- CONTROL: unpinned + down. The fixer MUST fire, or every "did not fire"
# assertion below would pass on a dispatch that is simply broken.
mod, row = _row(pinned=False, healthy=False)
check(row["status"] == "warn", "control unpinned-down: status is warn")
check("not running" in str(row.get("detail") or ""),
      "control unpinned-down: detail is the optional downgrade")
check(not row.get("restart_veto"), "control unpinned-down: carries no veto")
check(_fix_dispatch(mod, row) == ["screen-capture"],
      "control unpinned-down: --fix DOES dispatch the fixer")

# --- CONTROL: unpinned + healthy. Composition must not invent a veto.
mod, row = _row(pinned=False, healthy=True)
check(row["status"] == "ok", f"control unpinned-healthy: stays ok, got {row['status']}")
check(not row.get("restart_veto"), "control unpinned-healthy: carries no veto")

# --- PINNED + DOWN: veto reaches the detail AND stops the destructive act.
mod, row = _row(pinned=True, healthy=False)
detail = str(row.get("detail") or "")
check(row["status"] == "warn", "pinned-down: status is warn")
check("DO NOT RESTART screen-capture pid " + PID in detail,
      f"pinned-down: owner-facing detail carries the veto -- got {detail!r}")
check("DO NOT RESTART" in str(row.get("restart_veto") or ""),
      f"pinned-down: restart_veto is set structurally -- got {row.get('restart_veto')!r}")
check(_fix_dispatch(mod, row) == [],
      "pinned-down: --fix does NOT dispatch the listener-killing fixer")

# --- PINNED + HEALTHY: check_port returns bare `ok`, so this is the state
# whose pin was never evaluated at all.
mod, row = _row(pinned=True, healthy=True)
detail = str(row.get("detail") or "")
check(row["status"] == "warn",
      f"pinned-healthy: bare ok escalates to warn, got {row['status']}")
check("DO NOT RESTART screen-capture pid " + PID in detail,
      f"pinned-healthy: detail carries the veto -- got {detail!r}")
check("DO NOT RESTART" in str(row.get("restart_veto") or ""),
      f"pinned-healthy: restart_veto is set -- got {row.get('restart_veto')!r}")

print()
print("FAIL" if fail else "PASS -- optional-service rows compose their pin, and the "
      "screen-capture --fix dispatch obeys the structured veto")
sys.exit(fail)

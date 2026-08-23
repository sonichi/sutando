#!/usr/bin/env python3
"""sparrowd C0 — the thin shell is a process boundary and nothing more.

Covers: supervised start, crash restart with backoff, graceful stop,
single-instance refusal, control-socket round trip, and the adapter-edge
rule (the package module names no concrete worker; the launcher does).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.sparrowd import (  # noqa: E402
    ControlServer, SingleInstance, Supervisor, WorkerSpec)

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def wait_for(pred, timeout=8.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)

    # long-lived worker: runs until killed
    alive = tdp / "alive.py"
    alive.write_text("import time\nwhile True: time.sleep(0.2)\n")
    # crasher: exits immediately with rc 3
    crasher = tdp / "crash.py"
    crasher.write_text("raise SystemExit(3)\n")

    sleeps: list[float] = []

    def fake_sleep(s):
        sleeps.append(s)
        time.sleep(0.01)

    sup = Supervisor(
        [WorkerSpec("steady", [sys.executable, str(alive)]),
         WorkerSpec("flappy", [sys.executable, str(crasher)])],
        backoff_initial_s=0.5, sleep=fake_sleep)
    sup.start()

    check(wait_for(lambda: sup.status()["workers"]["steady"]["state"] == "running"),
          "steady worker reaches running with a live pid")
    check(sup.status()["workers"]["steady"]["pid"] is not None,
          "running worker exposes its pid in status")

    check(wait_for(lambda: sup.status()["workers"]["flappy"]["restarts"] >= 2),
          "crashing worker is restarted (crash loop supervised, not fatal)")
    check(sup.status()["workers"]["flappy"]["last_exit"] == 3,
          "status reports the worker's real exit code")
    check(len(sleeps) >= 2 and sleeps[1] > sleeps[0],
          "restart backoff grows between rapid crashes")

    # control socket round trip against the live supervisor
    stopped = threading.Event()
    ctl = ControlServer(tdp / "ctl.sock", sup, stopped.set)
    ctl.start()

    def ask(payload):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(tdp / "ctl.sock"))
        c.sendall((json.dumps(payload) + "\n").encode())
        resp = json.loads(c.makefile("r").readline())
        c.close()
        return resp

    st = ask({"op": "status"})
    check(st["ok"] and "steady" in st["workers"],
          "control op:status returns the worker table")
    check(ask({"op": "health"})["ok"], "control op:health answers")
    check(ask({"op": "nope"})["ok"] is False,
          "unknown op is refused, not crashed")
    check(ask({"op": "stop"})["stopping"] and stopped.wait(2),
          "control op:stop fires the shutdown callback")

    steady_pid = sup.status()["workers"]["steady"]["pid"]
    sup.stop(grace_s=5)

    def pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    check(steady_pid is not None and wait_for(lambda: not pid_alive(steady_pid), 5),
          "graceful stop actually terminates the child process")
    ctl.close()

    # single-instance: second acquire on the same lock refused, first released
    lock = tdp / "d" / "sparrowd.lock"
    a, b = SingleInstance(lock), SingleInstance(lock)
    check(a.acquire() is True, "first instance acquires the lock")
    check(b.acquire() is False, "second instance is refused while held")
    a.release()
    check(b.acquire() is True, "lock is reacquirable after release")
    b.release()

# spawn failure (argv does not exist) is a supervised backoff, not a crash
with tempfile.TemporaryDirectory() as td3:
    sl = []
    sup3 = Supervisor([WorkerSpec("ghost", [str(Path(td3) / "no-such-bin")])],
                      backoff_initial_s=0.2,
                      sleep=lambda x: (sl.append(x), time.sleep(0.01)))
    sup3.start()
    check(wait_for(lambda: len(sl) >= 2),
          "unspawnable worker keeps backing off instead of raising")
    check(sup3.status()["workers"]["ghost"]["state"] in ("backoff", "pending"),
          "unspawnable worker is visible in status as backoff")
    sup3.stop(grace_s=1)

# a worker that ignores SIGTERM is escalated to SIGKILL at the grace deadline
with tempfile.TemporaryDirectory() as td4:
    stubborn = Path(td4) / "stubborn.py"
    ready4 = Path(td4) / "handler-ready"
    stubborn.write_text(
        "import signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready4)!r}).touch()\n"
        "while True: time.sleep(0.2)\n")
    sup4 = Supervisor([WorkerSpec("stubborn", [sys.executable, str(stubborn)])])
    sup4.start()
    check(wait_for(ready4.exists),
          "SIGTERM-ignoring worker starts (handler installed before stop)")
    spid = sup4.status()["workers"]["stubborn"]["pid"]
    sup4.stop(grace_s=0.5)

    def pid_alive2(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    check(spid is not None and wait_for(lambda: not pid_alive2(spid), 5),
          "grace expiry escalates to SIGKILL — no orphan survives stop")

# garbage on the control socket is refused, never a crash; double close is safe
with tempfile.TemporaryDirectory() as td5:
    sup5 = Supervisor([])
    ctl5 = ControlServer(Path(td5) / "c.sock", sup5, lambda: None)
    ctl5.start()
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(str(Path(td5) / "c.sock"))
    c.sendall(b"this is not json\n")
    resp = json.loads(c.makefile("r").readline())
    c.close()
    check(resp["ok"] is False, "non-JSON control payload gets a clean refusal")
    ctl5.close()
    ctl5.close()  # second close: unlink path already gone, still silent
    check(True, "double close is idempotent")

# release() without acquire and double release are both no-ops
si = SingleInstance(Path(tempfile.mkdtemp()) / "x.lock")
si.release()
check(si.acquire() is True and (si.release() or si.release()) is None,
      "release is idempotent and safe before acquire")

# run() IN-PROCESS: a background client stops it once the socket appears —
# covers the entry path under coverage (the subprocess arm below cannot)
with tempfile.TemporaryDirectory() as td6:
    from ag2_sparrow.sparrowd import run as _run
    t6 = Path(td6)
    w6 = t6 / "w.py"; w6.write_text("import time\nwhile True: time.sleep(0.2)\n")
    sock6 = t6 / "state" / "sparrowd.sock"

    def stop_when_up():
        if not wait_for(sock6.exists, 8):
            return
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock6))
        c.sendall(b'{"op": "stop"}\n')
        c.makefile("r").readline()
        c.close()
    th = threading.Thread(target=stop_when_up, daemon=True)
    th.start()
    rc6 = _run([WorkerSpec("w6", [sys.executable, str(w6)])], t6 / "state",
               backoff_initial_s=0.2)
    th.join(timeout=5)
    check(rc6 == 0, "run() in-process returns 0 after socket-driven stop")
    # second run() against the SAME held lock path: exercise the refusal leg
    inst_hold = SingleInstance(t6 / "state" / "sparrowd.lock")
    check(inst_hold.acquire(), "test harness re-holds the lock")
    rc_dup = _run([], t6 / "state")
    check(rc_dup == 0, "run() with lock held exits 0 cleanly (no duplicate daemon)")
    inst_hold.release()

# REGRESSION (owner P0-1): the control plane stays available during backoff.
# Default (stop-interruptible) pause, long backoff — no injected sleep.
with tempfile.TemporaryDirectory() as td8:
    sup8 = Supervisor([WorkerSpec("ghost8", [str(Path(td8) / "missing-bin")])],
                      backoff_initial_s=30.0)
    sup8.start()
    check(wait_for(lambda: sup8.status()["workers"]["ghost8"]["state"] == "backoff"),
          "regression: worker enters backoff (spawn failure, default pause)")
    t0 = time.monotonic()
    st8 = sup8.status()
    dt_status = time.monotonic() - t0
    check(dt_status < 1.0 and st8["workers"]["ghost8"]["state"] == "backoff",
          f"regression: status() returns immediately mid-backoff ({dt_status*1000:.0f}ms)")
    t0 = time.monotonic()
    sup8.stop(grace_s=0)
    dt_stop = time.monotonic() - t0
    check(dt_stop < 2.0,
          f"regression: stop(grace=0) is immediate mid-backoff ({dt_stop*1000:.0f}ms)")

# REGRESSION (owner P0-2): a half-open client never starves the control plane.
with tempfile.TemporaryDirectory() as td9:
    sup9 = Supervisor([])
    stopped9 = threading.Event()
    ctl9 = ControlServer(Path(td9) / "c.sock", sup9, stopped9.set)
    ctl9.start()
    half = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    half.connect(str(Path(td9) / "c.sock"))
    half.sendall(b"{")  # no newline, held open
    time.sleep(0.1)
    t0 = time.monotonic()
    c9 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c9.connect(str(Path(td9) / "c.sock"))
    c9.sendall(b'{"op": "status"}\n')
    r9 = json.loads(c9.makefile("r").readline())
    dt9 = time.monotonic() - t0
    c9.close()
    check(r9["ok"] and dt9 < 1.0,
          f"regression: full status succeeds beside a half-open client ({dt9*1000:.0f}ms)")
    half.close()
    ctl9.close()

# the real run() entry, end to end in a subprocess: lock + socket + stop
with tempfile.TemporaryDirectory() as td2:
    t2 = Path(td2)
    w = t2 / "w.py"; w.write_text("import time\nwhile True: time.sleep(0.2)\n")
    runner = t2 / "runner.py"
    runner.write_text(f"""
import sys
sys.path.insert(0, {str(REPO / 'packages' / 'ag2-sparrow')!r})
from ag2_sparrow.sparrowd import WorkerSpec, run
raise SystemExit(run([WorkerSpec("w", [sys.executable, {str(w)!r}])], {str(t2 / 'state')!r}))
""")
    proc = subprocess.Popen([sys.executable, str(runner)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sock = t2 / "state" / "sparrowd.sock"
    check(wait_for(sock.exists, 8), "run(): control socket appears")

    def ask2(payload):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))
        c.sendall((json.dumps(payload) + "\n").encode())
        r = json.loads(c.makefile("r").readline()); c.close(); return r

    check(wait_for(lambda: ask2({"op": "status"})["workers"]["w"]["state"] == "running", 8),
          "run(): supervised worker running end to end")
    check(ask2({"op": "stop"})["stopping"], "run(): stop accepted over the socket")
    check(proc.wait(timeout=10) == 0, "run(): daemon exits 0 on requested stop")
    check(not sock.exists(), "run(): control socket removed on shutdown")

# a worker that ran long before crashing resets its backoff (injected clock)
with tempfile.TemporaryDirectory() as td7:
    once = Path(td7) / "once.py"; once.write_text("raise SystemExit(0)\n")
    t = {"now": 0.0}
    sl7 = []

    def clock7():
        # every read advances 31s, so spawn-to-exit always LOOKS long-lived
        t["now"] += 31
        return t["now"]

    sup7 = Supervisor([WorkerSpec("longrun", [sys.executable, str(once)])],
                      backoff_initial_s=0.4,
                      sleep=lambda x: (sl7.append(x), time.sleep(0.01)),
                      clock=clock7)
    sup7.start()
    check(wait_for(lambda: len(sl7) >= 3),
          "long-lived-run cycles keep restarting")
    check(all(abs(x - 0.4) < 1e-9 for x in sl7[:3]),
          "backoff resets to initial after a long-lived run (never climbs)")
    sup7.stop(grace_s=1)

# release() swallows a manually-closed fd (OSError arm)
si3 = SingleInstance(Path(tempfile.mkdtemp()) / "y.lock")
check(si3.acquire(), "acquire for the closed-fd release arm")
os.close(si3._fd)
si3.release()
check(True, "release after external close never raises")

# launcher module: worker_specs() is importable and names the real bridge
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("sparrowd_launcher", REPO / "src" / "sparrowd.py")
_l = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_l)
_specs = _l.worker_specs()
check(len(_specs) == 1 and _specs[0].name == "remote-gateway-bridge",
      "worker_specs() yields exactly the gateway bridge")
check(Path(_specs[0].argv[1]).exists(),
      "the spec's target script exists at the resolved path")
_calls = []
_l.run = lambda specs, sd: (_calls.append((specs, sd)), 0)[1]
_l.external_supervisor = lambda marker: None  # guard has its own tests below
check(_l.main() == 0 and _calls and str(_calls[0][1]).endswith("state/sparrowd"),
      "main() wires worker_specs into run with the workspace state dir")

# adapter edge: forbidden set derives from the adapter's specs so the
# guard grows with the worker list; static tokens have no spec-side source.
pkg = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "sparrowd.py").read_text()
_forbidden = {"chat.ag2.space", "discord", "telegram"}
for _s in _specs:
    _forbidden.add(_s.name)
    _forbidden.add(_s.name.replace("-", "_"))
    _forbidden.add(Path(_s.argv[1]).name)
for tok in sorted(_forbidden):
    check(tok not in pkg, f"package shell never names {tok!r} (specs injected)")

# ...and the launcher (the adapter) is where the concrete worker lives
launcher = (REPO / "src" / "sparrowd.py").read_text()
check("remote-gateway-bridge.py" in launcher and "worker_specs" in launcher,
      "launcher owns the concrete worker list")
check("resolve_workspace" in launcher,
      "launcher resolves state under the workspace helper, not a literal path")

# external-supervisor guard: a live process running the worker script must
# make the launcher refuse (dual supervision = eviction/reap loop)
import importlib.util as _ilu
import subprocess as _sp
import tempfile as _tf
import time as _time
_spec = _ilu.spec_from_file_location("sparrowd_launcher", REPO / "src" / "sparrowd.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
with _tf.TemporaryDirectory() as _td:
    dummy = Path(_td) / "guardtest-marker-bridge.py"
    dummy.write_text("import time\ntime.sleep(30)\n")
    proc = _sp.Popen([sys.executable, str(dummy)])
    try:
        _time.sleep(0.3)
        owned = _mod.external_supervisor(dummy.name)
        check(owned is not None and str(proc.pid) in owned,
              "guard detects a live externally-owned worker (pid reported)")
    finally:
        proc.kill(); proc.wait()
    _time.sleep(0.2)
    check(_mod.external_supervisor(dummy.name) is None,
          "guard clear when no such process exists")
check("external_supervisor" in launcher and "refusing to start" in launcher,
      "launcher main() wires the refuse-to-start guard")
_l.external_supervisor = lambda marker: "pid 999 (1 keepalive.sh)"
_ran = []
_l.run = lambda specs, sd: (_ran.append(1), 0)[1]
check(_l.main() == 2 and not _ran,
      "externally-owned worker: main refuses (rc 2) and never calls run()")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)

#!/usr/bin/env python3
"""Tests for startup's guarded voice-agent wedge recovery (impl plan
amendment T4 — kill-path inventory).

`startup.sh` must NOT reap a wedged :9900 listener with the generic
`lsof | xargs kill` path: killing whatever owns the port on a port match
alone is the unvalidated-kill class amendments S4/T4/U1 remove. The
`reap_wedged_voice_agent` function (src/startup-runtime.sh, called by
startup.sh) delegates the whole kill-and-replace transaction to ONE guarded
`voice-lock.py takeover` invocation.

Cases: wedged listener with a valid structured lock → killed under the guard,
lock unlinked; wedged listener whose lock names a NON-listener → blocked, no
signal sent, lock untouched; responsive listener → never probed for takeover;
no interpreter → fail closed, nothing signaled; wedged listener with NO lock →
takeover reports no-lock and nothing is signaled (an unvalidated :9900 owner
is never killed).

`curl` is a PATH shim (SHIM_CURL_RC drives wedged-vs-responsive) so the test
never waits out a real 10 s probe; `lsof` is real and sees real listeners.

Run: python3 tests/startup-voice-wedge-reap.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable or "python3"

failures = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def my_start_time_ms(pid):
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True
    ).stdout.strip()
    return int(time.mktime(time.strptime(out)) * 1000)


def structured_lock(pidfile, pid, start_ms, entry, workspace):
    pidfile.write_text(
        json.dumps(
            {
                "v": 1,
                "lockId": f"vl1-{uuid.uuid4()}",
                "pid": pid,
                "startTimeMs": start_ms,
                "entry": str(entry),
                "workspace": str(workspace),
            }
        )
        + "\n"
    )


def spawn_listener_group(port, entry_path):
    """A process whose argv names `entry_path` and which LISTENs on `port` —
    a stand-in voice-agent (same shape as tests/voice-lock.test.py)."""
    code = (
        "import socket, sys, time\n"
        "s = socket.socket()\n"
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "s.bind(('127.0.0.1', int(sys.argv[2])))\n"
        "s.listen(1)\n"
        "time.sleep(120)\n"
    )
    p = subprocess.Popen(
        [PY, "-c", code, str(entry_path), str(port)], start_new_session=True
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            c = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            c.close()
            return p
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("stand-in listener never bound")


def spawn_sleeper():
    return subprocess.Popen(
        [PY, "-c", "import time; time.sleep(120)"], start_new_session=True
    )


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    tmp = Path(tempfile.mkdtemp(prefix="startup-wedge-"))
    ws = tmp / "workspace"
    ws.mkdir()
    pidfile = ws / "state" / "locks" / "voice-agent.pid"
    pidfile.parent.mkdir(parents=True)
    shim_bin = tmp / "bin"
    shim_bin.mkdir()
    (shim_bin / "curl").write_text('#!/bin/bash\nexit "${SHIM_CURL_RC:-28}"\n')
    (shim_bin / "curl").chmod(0o755)
    entry = REPO / "src" / "voice-agent.ts"
    procs = []

    def run_reap(port, curl_rc=28, py=PY):
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{shim_bin}:{os.environ['PATH']}",
                "SHIM_CURL_RC": str(curl_rc),
                "REPO": str(REPO),
                "WORKSPACE": str(ws),
                "PY": py,
            }
        )
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$REPO/src/startup-runtime.sh"; reap_wedged_voice_agent "$1"',
                "_",
                str(port),
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    try:
        # --- wedged + valid lock → killed under the guard, lock unlinked ---
        print("wedged, validated owner:")
        port = free_port()
        agent = spawn_listener_group(port, entry)
        procs.append(agent)
        structured_lock(pidfile, agent.pid, my_start_time_ms(agent.pid), entry, ws)
        p = run_reap(port)
        check("reap exits 0", p.returncode == 0, p.stdout + p.stderr)
        check("guarded takeover reported", "guarded takeover of wedged voice-agent" in p.stdout, p.stdout)
        check("wedged owner killed", agent.wait(timeout=5) is not None)
        check("lock unlinked", not pidfile.exists())

        # --- wedged, lock names a NON-listener → blocked, no signal ---
        print("wedged, unvalidated lock:")
        port = free_port()
        listener = spawn_listener_group(port, entry)
        procs.append(listener)
        bystander = spawn_sleeper()
        procs.append(bystander)
        structured_lock(pidfile, bystander.pid, my_start_time_ms(bystander.pid), entry, ws)
        p = run_reap(port)
        check("reap still exits 0", p.returncode == 0, p.stdout + p.stderr)
        check("blocked is a WARN", "blocked/failed" in p.stdout, p.stdout)
        time.sleep(0.2)
        check("listener never signaled", listener.poll() is None)
        check("bystander never signaled", bystander.poll() is None)
        check("lock never removed", pidfile.exists())

        # --- wedged, NO lock → no-lock, nothing signaled ---
        print("wedged, no lock:")
        pidfile.unlink()
        p = run_reap(port)
        check("reap exits 0 with no lock", p.returncode == 0, p.stdout + p.stderr)
        check("no-lock reported", "no-lock" in p.stdout, p.stdout)
        time.sleep(0.2)
        check("unvalidated :port owner never killed", listener.poll() is None)

        # --- responsive listener → no takeover attempted ---
        print("responsive listener:")
        structured_lock(pidfile, listener.pid, my_start_time_ms(listener.pid), entry, ws)
        p = run_reap(port, curl_rc=0)
        check("responsive: reap exits 0", p.returncode == 0, p.stdout + p.stderr)
        check("responsive: no takeover attempted", "takeover" not in p.stdout, p.stdout)
        check("responsive: listener untouched", listener.poll() is None)
        check("responsive: lock untouched", pidfile.exists())

        # --- no interpreter → fail closed, nothing signaled ---
        print("no interpreter:")
        p = run_reap(port, py="")
        check("fail-closed exits 0", p.returncode == 0, p.stdout + p.stderr)
        check("fail-closed warns", "fail closed" in p.stdout, p.stdout)
        time.sleep(0.2)
        check("fail-closed sends no signal", listener.poll() is None)
        check("fail-closed leaves the lock", pidfile.exists())

        # --- startup.sh wiring: the :9900 call site uses the guarded reap ---
        startup = (REPO / "src" / "startup.sh").read_text()
        check(
            "startup.sh calls reap_wedged_voice_agent for :9900",
            "reap_wedged_voice_agent 9900" in startup,
        )
        check(
            "startup.sh no longer generic-reaps :9900",
            "reap_wedged_listener 9900" not in startup,
        )
    finally:
        for proc in procs:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print("\nAll startup wedge-recovery tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

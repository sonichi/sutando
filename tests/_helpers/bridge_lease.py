"""A child process that holds a gateway bridge's singleton lease through the production writer.

It takes the lease with `workspace_lock.acquire`, refreshes it with `workspace_lock.heartbeat`
and gives it back with `workspace_lock.release` at exit after SIGTERM, as remote_gateway_bridge
does (`_acquire_singleton`, `_lock_heartbeat_loop`, `_exit_on_signal`). Modes:
  exit    SIGTERM ends it and the lease is released (the bridge's normal stop)
  ignore  SIGTERM is ignored and the lease stays held (a bridge that will not stop)
  retain  SIGTERM ends it but the lease is retained (a publication still in flight)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _helpers.hermetic_env import isolated_child_env

SRC = Path(__file__).resolve().parent.parent.parent / "src"

SCRIPT = r"""
import atexit, signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import workspace_lock as wl
ws, role, mode, delay = Path(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
time.sleep(delay)
r = wl.acquire(role, ws)
if r.status == "deferred":
    print("deferred", flush=True)
    sys.exit(75)
if mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    atexit.register((wl.retain if mode == "retain" else wl.release), role, ws)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
print("held", flush=True)
while True:
    time.sleep(0.2)
    if not wl.heartbeat(role, ws):
        sys.exit(3)
"""


def role_for(instance: str) -> str:
    return "gateway-bridge" + (f".{instance}" if instance else "")


def start(workspace: Path, instance: str, *, mode: str = "exit", delay: float = 0.0,
          wait: bool = True) -> subprocess.Popen:
    """The child; with `wait`, only once it says it holds the lease."""
    proc = subprocess.Popen([sys.executable, "-c", SCRIPT, str(SRC), str(workspace), role_for(instance),
                             mode, str(delay)], stdout=subprocess.PIPE, env=isolated_child_env(), text=True)
    if wait:
        line = proc.stdout.readline().strip()
        if line != "held":
            proc.kill()
            proc.wait()
            raise RuntimeError(f"lease child said {line!r}")
    return proc


def spawner(workspace: Path, *, mode: str = "exit", delay: float = 0.0, started=None):
    """A `spawn` for connections.launch that starts a lease child for the env's GATEWAY_INSTANCE."""
    def spawn(argv, env, log_path):
        proc = start(workspace, env["GATEWAY_INSTANCE"], mode=mode, delay=delay, wait=False)
        if started is not None:
            started.append(proc)
        return proc
    return spawn


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    if proc.stdout:
        proc.stdout.close()

"""sparrowd — thin daemon shell for Sparrow's resident loops (C0).

Scope is deliberately a PROCESS BOUNDARY and nothing else: single-instance
lifecycle, a supervisor that keeps injected workers alive, graceful shutdown,
crash restart with backoff, a local control socket, and unified logging.
It does not define task states, touch delivery semantics, or name any
concrete worker — specs are injected by the launcher at the adapter edge.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


def _log(msg: str) -> None:
    print(f"[sparrowd] {msg}", flush=True)


@dataclass
class WorkerSpec:
    """One resident loop to keep alive. argv is executed as a child process;
    the shell never inspects or reinterprets what the worker does."""
    name: str
    argv: List[str]
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None


@dataclass
class _WorkerState:
    spec: WorkerSpec
    proc: Optional[subprocess.Popen] = None
    restarts: int = 0
    started_at: float = 0.0
    last_exit: Optional[int] = None
    state: str = "pending"  # pending | running | backoff | stopped


class SingleInstance:
    """flock-based exclusivity. Refusal is loud and non-destructive: the
    caller decides whether to exit; nothing is killed on contention."""

    def __init__(self, lock_path: Path):
        self._path = Path(lock_path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class Supervisor:
    """Keeps injected workers alive; restart-with-backoff on crash.

    `sleep` and `clock` are injected so tests drive time deterministically —
    the portability discipline the future core will inherit."""

    def __init__(self, specs: List[WorkerSpec], *,
                 backoff_initial_s: float = 1.0, backoff_max_s: float = 60.0,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._workers = {s.name: _WorkerState(spec=s) for s in specs}
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._sleep = sleep
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []

    def _pause(self, seconds: float) -> None:
        """Backoff wait: stop-interruptible by default; an injected sleep
        (tests) is honored but stop is still checked around it."""
        if self._sleep is not None:
            if not self._stop.is_set():
                self._sleep(seconds)
            return
        self._stop.wait(seconds)

    def start(self) -> None:
        for name in self._workers:
            t = threading.Thread(target=self._run_worker, args=(name,),
                                 name=f"sup:{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def _spawn(self, ws: _WorkerState) -> None:
        ws.proc = subprocess.Popen(
            ws.spec.argv, cwd=ws.spec.cwd,
            env={**os.environ, **(ws.spec.env or {})})
        ws.started_at = self._clock()
        ws.state = "running"
        _log(f"worker {ws.spec.name}: started pid {ws.proc.pid}")

    def _run_worker(self, name: str) -> None:
        ws = self._workers[name]
        backoff = self._backoff_initial
        while not self._stop.is_set():
            spawn_err = None
            with self._lock:
                try:
                    self._spawn(ws)
                except OSError as e:
                    ws.state = "backoff"
                    ws.last_exit = None
                    spawn_err = e
            if spawn_err is not None:
                _log(f"worker {name}: spawn failed ({spawn_err}) — backoff {backoff:.1f}s")
                self._pause(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue
            rc = ws.proc.wait()
            with self._lock:
                ws.last_exit = rc
                if self._stop.is_set():
                    ws.state = "stopped"
                    return
                # A long-lived run earns a reset; only rapid crash loops climb.
                if self._clock() - ws.started_at > 30:
                    backoff = self._backoff_initial
                ws.restarts += 1
                ws.state = "backoff"
            _log(f"worker {name}: exited rc={rc} — restart in {backoff:.1f}s "
                 f"(restart #{ws.restarts})")
            self._pause(backoff)
            backoff = min(backoff * 2, self._backoff_max)
        with self._lock:
            ws.state = "stopped"

    def status(self) -> dict:
        with self._lock:
            return {"workers": {
                n: {"state": ws.state,
                    "pid": ws.proc.pid if ws.proc and ws.proc.poll() is None else None,
                    "restarts": ws.restarts,
                    "last_exit": ws.last_exit}
                for n, ws in self._workers.items()}}

    def stop(self, grace_s: float = 10.0) -> None:
        self._stop.set()
        with self._lock:
            procs = [ws.proc for ws in self._workers.values()
                     if ws.proc and ws.proc.poll() is None]
        for p in procs:
            try:
                p.send_signal(signal.SIGTERM)
            except OSError:  # pragma: no cover - worker died between poll and signal
                pass
        deadline = self._clock() + grace_s
        for p in procs:
            remaining = max(0.0, deadline - self._clock())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _log(f"pid {p.pid}: did not exit in {grace_s}s — SIGKILL")
                p.kill()
        _log("all workers stopped")


class ControlServer:
    """Local Unix-socket control plane. One JSON object per line in, one out.
    Ops: status | health | stop. Transport only — no policy, no state."""

    def __init__(self, sock_path: Path, supervisor: Supervisor,
                 on_stop: Callable[[], None]):
        self._path = Path(sock_path)
        self._sup = supervisor
        self._on_stop = on_stop
        self._srv: Optional[socket.socket] = None

    def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(self._path))
        os.chmod(self._path, 0o600)
        self._srv.listen(4)
        threading.Thread(target=self._serve, name="control", daemon=True).start()
        _log(f"control socket at {self._path}")

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            # Per-connection thread: a half-open or slow client must never
            # starve the accept loop (availability of stop/status IS the point).
            threading.Thread(target=self._handle, args=(conn,),
                             name="control-conn", daemon=True).start()

    _MAX_REQUEST = 4096

    def _handle(self, conn) -> None:
        with conn:
            try:
                conn.settimeout(2.0)
                buf = b""
                while b"\n" not in buf and len(buf) <= self._MAX_REQUEST:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b"\n", 1)[0][: self._MAX_REQUEST]
                req = json.loads(line) if line.strip() else {}
            except (ValueError, OSError):
                req = {}
            op = req.get("op")
            if op == "status":
                resp = {"ok": True, **self._sup.status()}
            elif op == "health":
                st = self._sup.status()["workers"]
                healthy = all(w["state"] in ("running", "pending")
                              for w in st.values()) if st else True
                resp = {"ok": True, "healthy": healthy}
            elif op == "stop":
                resp = {"ok": True, "stopping": True}
            else:
                resp = {"ok": False, "error": f"unknown op {op!r}"}
            try:
                conn.sendall((json.dumps(resp) + "\n").encode())
            except OSError:
                pass
            if op == "stop":
                self._on_stop()

    def close(self) -> None:
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:  # pragma: no cover - already-closed socket on double close
                pass
        try:
            self._path.unlink()
        except OSError:
            pass


def run(specs: List[WorkerSpec], state_dir: Path,
        backoff_initial_s: float = 1.0) -> int:
    """Daemon entry: lock, supervise, serve control, wait for stop signal."""
    state_dir = Path(state_dir)
    inst = SingleInstance(state_dir / "sparrowd.lock")
    if not inst.acquire():
        _log("another sparrowd holds the lock — exiting cleanly")
        return 0
    stop_evt = threading.Event()
    sup = Supervisor(specs, backoff_initial_s=backoff_initial_s)
    ctl = ControlServer(state_dir / "sparrowd.sock", sup, stop_evt.set)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_evt.set())
    _log(f"starting {len(specs)} worker(s): {', '.join(s.name for s in specs)}")
    ctl.start()
    sup.start()
    stop_evt.wait()
    _log("shutdown requested")
    sup.stop()
    ctl.close()
    inst.release()
    return 0

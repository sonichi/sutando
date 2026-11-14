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

from .control_socket import control_socket_path


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
    revision: str = ""  # opaque to the shell: a spec that differs under the same name is a restart


@dataclass
class _WorkerState:
    spec: WorkerSpec
    proc: Optional[subprocess.Popen] = None
    restarts: int = 0
    started_at: float = 0.0
    last_exit: Optional[int] = None
    state: str = "pending"  # pending | running | backoff | stopped
    stop: threading.Event = field(default_factory=threading.Event)
    stop_at: float = 0.0  # when this one worker was told to stop (sync); 0 until then
    # every older process for this name still alive: never two processes for one name
    after: List["_WorkerState"] = field(default_factory=list)


def _running(ws: _WorkerState) -> bool:
    return ws.proc is not None and ws.proc.poll() is None


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
                 remove_grace_s: float = 10.0,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._workers = {s.name: _WorkerState(spec=s) for s in specs}
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._remove_grace = remove_grace_s
        self._sleep = sleep
        self._clock = clock
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        # Removed by sync but not yet exited: still owned, so stop() reaps them too.
        self._draining: List[_WorkerState] = []

    def _pause(self, ws: _WorkerState, seconds: float) -> None:
        """Backoff wait: stop-interruptible by default; an injected sleep
        (tests) is honored but stop is still checked around it."""
        if self._sleep is not None:
            if not ws.stop.is_set():
                self._sleep(seconds)
            return
        ws.stop.wait(seconds)

    def _start_thread(self, ws: _WorkerState) -> None:
        t = threading.Thread(target=self._run_worker, args=(ws,),
                             name=f"sup:{ws.spec.name}", daemon=True)
        t.start()
        self._threads.append(t)

    def start(self) -> None:
        for ws in list(self._workers.values()):
            self._start_thread(ws)

    def sync(self, specs: List[WorkerSpec]) -> "tuple[list, list]":
        """Make the supervised set equal `specs`: a new name is started, a name
        no longer listed is stopped (SIGTERM, SIGKILL past the grace) and stays
        owned until it has exited, a name whose spec changed is both: its
        replacement starts once every older process for the name has exited (a
        replacement that never started is dropped). Returns (added names,
        removed names); a changed name is in both."""
        want = {s.name: s for s in specs}
        with self._lock:
            removed = [self._workers.pop(n) for n in list(self._workers)
                       if n not in want or self._workers[n].spec != want[n]]
            added = [self._workers.setdefault(n, _WorkerState(spec=s))
                     for n, s in want.items() if n not in self._workers]
            self._draining = [ws for ws in self._draining + removed if _running(ws)]
            for ws in added:
                ws.after = [d for d in self._draining if d.spec.name == ws.spec.name]
            for ws in removed:
                ws.stop.set()
                ws.stop_at = self._clock()
                if ws.proc is not None and ws.proc.poll() is None:
                    try:
                        ws.proc.send_signal(signal.SIGTERM)
                    except OSError:  # pragma: no cover - died between poll and signal
                        pass
        for ws in removed:
            _log(f"worker {ws.spec.name}: {'changed — restarting' if ws.spec.name in want else 'removed — stopping'}")
        changed = {ws.spec.name for ws in removed}
        for ws in added:
            if ws.spec.name not in changed:
                _log(f"worker {ws.spec.name}: added")
            if not self._stop.is_set():
                self._start_thread(ws)
        return [ws.spec.name for ws in added], [ws.spec.name for ws in removed]

    def _wait(self, ws: _WorkerState) -> int:
        """The worker's exit code; a worker removed by sync that outlives
        the grace is killed here, on its own thread."""
        while True:
            try:
                return ws.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if ws.stop_at and self._clock() - ws.stop_at > self._remove_grace:
                    _log(f"pid {ws.proc.pid}: did not exit in {self._remove_grace}s — SIGKILL")
                    ws.proc.kill()

    def _spawn(self, ws: _WorkerState) -> None:
        ws.proc = subprocess.Popen(
            ws.spec.argv, cwd=ws.spec.cwd,
            env={**os.environ, **(ws.spec.env or {})})
        ws.started_at = self._clock()
        ws.state = "running"
        _log(f"worker {ws.spec.name}: started pid {ws.proc.pid}")

    def _run_worker(self, ws: _WorkerState) -> None:
        name = ws.spec.name
        backoff = self._backoff_initial
        while any(_running(old) for old in ws.after) and not ws.stop.wait(0.05):
            pass
        while not ws.stop.is_set():
            spawn_err = None
            with self._lock:
                # sync stops a worker under this lock: a stop set here is seen
                # before the spawn, a spawn before the stop leaves a pid to signal.
                if ws.stop.is_set():
                    break
                try:
                    self._spawn(ws)
                except OSError as e:
                    ws.state = "backoff"
                    ws.last_exit = None
                    spawn_err = e
            if spawn_err is not None:
                _log(f"worker {name}: spawn failed ({spawn_err}) — backoff {backoff:.1f}s")
                self._pause(ws, backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue
            rc = self._wait(ws)
            with self._lock:
                ws.last_exit = rc
                if ws.stop.is_set():
                    ws.state = "stopped"
                    return
                # A long-lived run earns a reset; only rapid crash loops climb.
                if self._clock() - ws.started_at > 30:
                    backoff = self._backoff_initial
                ws.restarts += 1
                ws.state = "backoff"
            _log(f"worker {name}: exited rc={rc} — restart in {backoff:.1f}s "
                 f"(restart #{ws.restarts})")
            self._pause(ws, backoff)
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
                for n, ws in self._workers.items()},
                "draining": [ws.spec.name for ws in self._draining if _running(ws)]}

    def stop(self, grace_s: float = 10.0) -> None:
        """Every owned process is reaped before this returns: the supervised
        workers and the ones a sync removed that have not exited yet."""
        self._stop.set()
        with self._lock:
            for ws in self._workers.values():
                ws.stop.set()
            procs = [ws.proc for ws in list(self._workers.values()) + self._draining
                     if _running(ws)]
            self._draining = []
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
                p.wait()
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
        backoff_initial_s: float = 1.0,
        rescan: Optional[Callable[[], List[WorkerSpec]]] = None,
        rescan_s: float = 30.0, grace_s: float = 10.0,
        wait_for_lock: bool = False, lock_poll_s: float = 5.0) -> int:
    """Daemon entry: lock, supervise, serve control, wait for stop signal.
    With `rescan`, every `rescan_s` the supervised set is synced to what it
    returns: the launcher decides what exists, the shell only follows.
    A rescan that raises is a scan that could not be made, not an empty one:
    the current set is kept, logged once until the scan recovers.
    With `wait_for_lock`, a held lock is waited for (one line when the wait
    starts, one when it ends) instead of exiting, so a relauncher never loops."""
    state_dir = Path(state_dir)
    inst = SingleInstance(state_dir / "sparrowd.lock")
    acquired = inst.acquire()
    if not acquired and not wait_for_lock:
        _log("another sparrowd holds the lock — exiting cleanly")
        return 0
    stop_evt = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop_evt.set())
    if not acquired:
        _log("another sparrowd holds the lock — waiting for it")
        while not inst.acquire():
            if stop_evt.wait(lock_poll_s):
                _log("stopped while waiting for the lock")
                return 0
        _log("lock acquired")
    sup = Supervisor(specs, backoff_initial_s=backoff_initial_s, remove_grace_s=grace_s)
    _log(f"starting {len(specs)} worker(s): {', '.join(s.name for s in specs)}")
    ctl: Optional[ControlServer] = None
    try:
        ctl = ControlServer(control_socket_path(state_dir, create=True), sup, stop_evt.set)
        ctl.start()
    except OSError as e:
        # Exiting here is a relaunch loop under any supervisor; the workers run, signals still stop us.
        _log(f"control socket unavailable ({e}) — supervising without it; stop with SIGTERM")
        if ctl is not None:
            ctl.close()
        ctl = None
    sup.start()
    unavailable = False
    while not stop_evt.wait(rescan_s if rescan is not None else None):
        try:
            want = rescan()
        except Exception as e:  # a launcher fault must not take the running workers down
            if not unavailable:
                _log(f"rescan unavailable ({type(e).__name__}: {e}) — keeping the current workers until it recovers")
            unavailable = True
            continue
        if unavailable:
            _log("rescan recovered")
        unavailable = False
        sup.sync(want)
    _log("shutdown requested")
    sup.stop(grace_s)
    if ctl is not None:
        ctl.close()
    inst.release()
    return 0

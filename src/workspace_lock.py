#!/usr/bin/env python3
"""Atomic per-workspace role lock for sutando singleton enforcement (MC1).

Closes the real dual-poller bug class (confirmed by air, 2026-07-16): NOT a
second `claude --name sutando-core` (start-cli.sh's `core_claude_running` pgrep
already guards that), but **two gateway bridges / supervisors polling one
relay** → duplicate task delivery. Two observed incidents:
  1. An orphaned bridge from a PRIOR install (ppid 1, outlived its parent by
     days) still polling alongside the live app bridge → owner message
     delivered twice.
  2. A replacement bridge + Electron respawn starting simultaneously → two
     pollers on one bearer (a TOCTOU race).

This primitive gives the bridge/supervisor a role lock that closes both:
  • **Atomic acquire** (`O_CREAT|O_EXCL`) → the simultaneous-start race can only
    produce one winner (incident #2).
  • **Held + heartbeated** (mtime-style freshness in the payload, like
    `state/cores/<host>.alive`) → a would-be second poller distinguishes a
    LIVE holder (defer) from a STALE/orphaned holder (reap + take), so a
    crashed or hung holder cannot wedge the role forever (incident #1's
    ungraceful/orphan case).

Liveness = **heartbeat freshness**, deliberately NOT pid-alive: the 83188 ghost
was alive-but-stale, and a hung-but-running holder that stopped heartbeating
should lose the lock. pid recycling would also make kill(0) unreliable.

Lock file: `<workspace>/state/locks/<role>.lock`
  {"role","pid","host","workspace","acquired_at","heartbeat_at","schema_version":1}

The lock is per-`(workspace, role)`: the file lives under the workspace (so it
is inherently workspace-scoped — the contended resource is the relay/task bus
of THAT workspace) and `role` separates `gateway-bridge` from `supervisor`.

TRANSITION CAVEAT: a pre-lock orphan (old code with no lock support) won't
respect this — during an upgrade the installer/supervisor must still kill old
pollers. The lock makes the steady state (both sides lock-aware) correct.

Python API (bridge/supervisor import this directly):
    r = acquire("gateway-bridge")        # LockResult(status=..., holder=...)
    if r.status == "deferred": ...defer/exit...
    heartbeat("gateway-bridge")          # call ~every 30s while holding
    release("gateway-bridge")            # on shutdown

CLI (bash consumers, e.g. supervisor):
    workspace_lock.py acquire   --role R [--workspace W] [--stale-seconds N]
       exit 0 acquired/reaped (holder line on stdout) | exit 3 deferred (holder
       json on stdout) | exit 0 also on unexpected error (fail-open — never
       wedge startup on a lock bug; a dropped guard only risks the pre-existing
       dual-poller, never a false refuse of the only poller).
    workspace_lock.py heartbeat --role R [--workspace W]   exit 0 held / 1 lost
    workspace_lock.py release   --role R [--workspace W]
    workspace_lock.py status    --role R [--workspace W]   holder json / empty
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_lock import lock_fd, unlock_fd  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_STALE_SECONDS = 90  # same freshness window as state/cores/<host>.alive


def _resolve_workspace(override: str | None) -> Path:
    if override:
        return Path(override)
    from workspace_default import resolve_workspace  # noqa: E402
    return resolve_workspace()


def _host_label() -> str:
    try:
        from util_paths import _host_label as hl  # noqa: E402
        return hl()
    except Exception:  # pragma: no cover - host-label fallback
        return socket.gethostname().split(".")[0]


def _now() -> int:
    return int(time.time())


def _locks_dir(workspace: Path) -> Path:
    return workspace / "state" / "locks"


def _lock_path(workspace: Path, role: str) -> Path:
    return _locks_dir(workspace) / f"{role}.lock"


@contextlib.contextmanager
def _guard(workspace: Path, role: str):
    """Serialize a lock's read-decide-write critical section with an exclusive
    advisory lock on a persistent sidecar guard file (never unlinked). This is
    what makes reap+acquire and heartbeat mutually exclusive, closing the
    heartbeat-overwrites-a-freshly-reaped-owner race: a slow holder that resumes
    into heartbeat() cannot interleave between another process's reap and
    _try_create, because both must first hold this flock. Same-host by design
    (the lock is per-host), so flock's local-FS semantics are reliable here."""
    _locks_dir(workspace).mkdir(parents=True, exist_ok=True)
    gp = _locks_dir(workspace) / f"{role}.lock.guard"
    fd = os.open(str(gp), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        lock_fd(fd)
        yield
    finally:
        try:
            unlock_fd(fd)
        finally:
            os.close(fd)


class LockResult:
    """Result of acquire(): status is 'acquired' | 'reaped' | 'deferred';
    holder is the LIVE holder's payload when deferred (else None)."""
    def __init__(self, status, holder=None):
        self.status = status
        self.holder = holder

    def __repr__(self):
        return f"LockResult(status={self.status!r}, holder={self.holder!r})"


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except FileNotFoundError:  # pragma: no cover - unlink race
        return None
    except Exception:  # pragma: no cover - defensive fail-safe
        return None  # corrupt → treat as absent/reapable


def _payload(role: str, workspace: Path) -> dict:
    now = _now()
    return {
        "role": role,
        "pid": os.getpid(),
        "host": _host_label(),
        "workspace": str(workspace),
        "acquired_at": now,
        "heartbeat_at": now,
        "schema_version": SCHEMA_VERSION,
    }


def _is_fresh(holder: dict, stale_seconds: int) -> bool:
    hb = holder.get("heartbeat_at")
    if not isinstance(hb, (int, float)):
        return False  # no/invalid heartbeat → stale
    return (_now() - hb) < stale_seconds


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".lock.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def acquire(role: str, workspace: Path | str | None = None,
            stale_seconds: int = DEFAULT_STALE_SECONDS) -> LockResult:
    ws = workspace if isinstance(workspace, Path) else _resolve_workspace(workspace)
    path = _lock_path(ws, role)
    data = _payload(role, ws)
    # The whole read-decide-write runs under the guard flock, so no other
    # acquire/heartbeat/release can interleave — the reap→take is atomic.
    with _guard(ws, role):
        holder = _read(path)
        if holder is None:
            # absent, or corrupt/unreadable → treat as free and take it
            _write_atomic(path, data)
            return LockResult("acquired")
        if holder.get("pid") == os.getpid() and holder.get("host") == data["host"]:
            # idempotent re-acquire — refresh, preserving the original generation
            data["acquired_at"] = holder.get("acquired_at", data["acquired_at"])
            _write_atomic(path, data)
            return LockResult("acquired")
        if _is_fresh(holder, stale_seconds):
            return LockResult("deferred", holder=holder)
        # stale / orphaned holder → reap and take it (atomically, under guard)
        _write_atomic(path, data)
        return LockResult("reaped")


def heartbeat(role: str, workspace: Path | str | None = None) -> bool:
    """Refresh heartbeat_at iff we are STILL the current holder. Returns False
    (without writing) if we've been reaped — under the guard flock, so it cannot
    clobber a holder that took over between our read and write (the P1 race)."""
    ws = workspace if isinstance(workspace, Path) else _resolve_workspace(workspace)
    path = _lock_path(ws, role)
    with _guard(ws, role):
        holder = _read(path)
        if not holder or holder.get("pid") != os.getpid() or holder.get("host") != _host_label():
            return False
        holder["heartbeat_at"] = _now()
        _write_atomic(path, holder)
        return True


def release(role: str, workspace: Path | str | None = None) -> None:
    """Remove the lock iff we still hold it (pid+host match), under the guard."""
    ws = workspace if isinstance(workspace, Path) else _resolve_workspace(workspace)
    path = _lock_path(ws, role)
    with _guard(ws, role):
        holder = _read(path)
        if holder and holder.get("pid") == os.getpid() and holder.get("host") == _host_label():
            try:
                os.unlink(path)
            except FileNotFoundError:  # pragma: no cover - unlink race
                pass


def read_holder(role: str, workspace: Path | str | None = None) -> dict | None:
    ws = workspace if isinstance(workspace, Path) else _resolve_workspace(workspace)
    return _read(_lock_path(ws, role))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="workspace_lock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("acquire", "heartbeat", "release", "status"):
        p = sub.add_parser(name)
        p.add_argument("--role", required=True)
        p.add_argument("--workspace", default=None)
        if name == "acquire":
            p.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS)
    args = ap.parse_args(argv)
    try:
        ws = _resolve_workspace(args.workspace)
        if args.cmd == "acquire":
            r = acquire(args.role, ws, args.stale_seconds)
            if r.status == "deferred":
                json.dump({"deferred": True, "holder": r.holder}, sys.stdout)
                sys.stdout.write("\n")
                return 3
            sys.stdout.write(r.status + "\n")
            return 0
        if args.cmd == "heartbeat":
            return 0 if heartbeat(args.role, ws) else 1
        if args.cmd == "release":
            release(args.role, ws)
            return 0
        if args.cmd == "status":
            h = read_holder(args.role, ws)
            if h:
                json.dump(h, sys.stdout)
                sys.stdout.write("\n")
            return 0
    except Exception as e:  # fail-open on the CLI path  # pragma: no cover - CLI fail-open
        sys.stderr.write(f"workspace_lock: {args.cmd} error ({e}) — proceeding\n")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

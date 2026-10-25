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

Liveness = **heartbeat freshness**: the 83188 ghost was alive-but-stale, and a
hung-but-running holder that stopped heartbeating should lose the lock. A live
pid is therefore never proof of ownership (pid recycling). One narrowing: a
holder recorded on THIS host whose pid no longer exists cannot be polling, so it
is reaped at once instead of blocking a successor for the whole stale window.
A holder on another host is judged by heartbeat alone, and a holder that called
`retain()` (it exits on purpose while still owning the role) ages out normally.
Residual risk: the pid rule here ignores the process start time, so a dead holder
whose pid was already reused reads as alive and falls back to the heartbeat rule.
The record also carries `proc_start` (boot id + start time, `proc_start()`: /proc on
Linux, sysctl on macOS), so a caller about to signal the holder can prove the pid is
still the process that wrote the record.

Lock file: `<workspace>/state/locks/<role>.lock`
  {"role","pid","host","workspace","acquired_at","heartbeat_at","schema_version":1,
   "proc_start" (where the start time can be read)}

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
    retain("gateway-bridge")             # exiting but keep the role until stale

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
import struct
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


_HOST_LABEL: str | None = None


def _host_label() -> str:
    # First value wins: a later sys.path change must not rename a lock this process holds.
    global _HOST_LABEL
    if _HOST_LABEL is None:
        try:
            from util_paths import _host_label as hl  # noqa: E402
            _HOST_LABEL = hl()
        except Exception:  # pragma: no cover - host-label fallback
            _HOST_LABEL = socket.gethostname().split(".")[0]
    return _HOST_LABEL


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


def _darwin_sysctl(mib: list) -> bytes | None:
    """The raw value of sysctl `mib` on macOS; None when it cannot be read or is empty."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        arr, size = (ctypes.c_int * len(mib))(*mib), ctypes.c_size_t(0)
        if libc.sysctl(arr, len(mib), None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(arr, len(mib), buf, ctypes.byref(size), None, 0) != 0:
            return None
        return buf.raw[:size.value] or None
    except (OSError, AttributeError, TypeError):
        return None


# CTL_KERN, then KERN_PROC/KERN_PROC_PID, KERN_BOOTTIME and KERN_PROCARGS2 (<sys/sysctl.h>).
_KERN, _KERN_PROC_PID, _KERN_BOOTTIME, _KERN_PROCARGS2 = 1, (14, 1), 21, 49


def proc_argv(pid: int) -> list[str] | None:
    """The argv of `pid` on macOS (KERN_PROCARGS2); None elsewhere or when unreadable."""
    if sys.platform != "darwin":
        return None
    raw = _darwin_sysctl([_KERN, _KERN_PROCARGS2, pid])
    if raw is None or len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    _exe, _, rest = raw[4:].partition(b"\0")
    args = rest.lstrip(b"\0").split(b"\0")
    return [a.decode(errors="replace") for a in args[:argc]] if 0 < argc <= len(args) else None


def proc_start(pid: int, proc: Path = Path("/proc")) -> str | None:
    """`<boot id>:<start time>` of `pid`, which no later process with that pid shares:
    /proc's start ticks, or on macOS kinfo_proc's p_starttime; None where it cannot be
    read (or the process is gone)."""
    if sys.platform == "darwin" and not proc.is_dir():
        boot = _darwin_sysctl([_KERN, _KERN_BOOTTIME])
        info = _darwin_sysctl([_KERN, *_KERN_PROC_PID, pid])
        if boot is None or info is None or len(boot) < 12 or len(info) < 12:
            return None
        # struct timeval {time_t; suseconds_t} opens both kern.boottime and kinfo_proc.
        (bsec, busec), (sec, usec) = (struct.unpack_from("=qi", b) for b in (boot, info))
        return f"darwin-{bsec}.{busec:06d}:{sec}.{usec:06d}" if sec > 0 else None
    try:
        boot = (proc / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
        stat = (proc / str(pid) / "stat").read_bytes()
    except (OSError, ValueError):
        return None
    fields = stat[stat.rfind(b")") + 1:].split()
    # starttime is field 22 of stat(5); fields here start at field 3 (state).
    ticks = fields[19].decode() if len(fields) > 19 and fields[19].isdigit() else ""
    return f"{boot}:{ticks}" if boot and ticks else None


def _payload(role: str, workspace: Path) -> dict:
    now = _now()
    data = {
        "role": role,
        "pid": os.getpid(),
        "host": _host_label(),
        "workspace": str(workspace),
        "acquired_at": now,
        "heartbeat_at": now,
        "schema_version": SCHEMA_VERSION,
    }
    start = proc_start(os.getpid())
    if start:
        data["proc_start"] = start
    return data


def _is_fresh(holder: dict, stale_seconds: int) -> bool:
    hb = holder.get("heartbeat_at")
    if not isinstance(hb, (int, float)):
        return False  # no/invalid heartbeat → stale
    return (_now() - hb) < stale_seconds


def _holder_is_dead(holder: dict, host: str) -> bool:
    """True only for a non-retained holder on THIS host whose pid is gone."""
    if os.name == "nt" or holder.get("host") != host or holder.get("retained"):
        return False  # os.kill(pid, 0) terminates the process on Windows
    pid = holder.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # EPERM: the pid exists under another uid
        return False
    return False


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
        if _is_fresh(holder, stale_seconds) and not _holder_is_dead(holder, data["host"]):
            return LockResult("deferred", holder=holder)
        # stale, or a dead same-host holder → reap and take it (atomically, under guard)
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


def retain(role: str, workspace: Path | str | None = None) -> bool:
    """Mark our lock to outlive this process: it then ages out by heartbeat
    instead of being reaped as soon as our pid is gone. False if not ours."""
    ws = workspace if isinstance(workspace, Path) else _resolve_workspace(workspace)
    path = _lock_path(ws, role)
    with _guard(ws, role):
        holder = _read(path)
        if not holder or holder.get("pid") != os.getpid() or holder.get("host") != _host_label():
            return False
        holder["retained"] = True
        _write_atomic(path, holder)
        return True


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

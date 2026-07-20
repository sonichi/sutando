#!/usr/bin/env python3
"""Per-host heartbeat for sutando-core sessions.

Writes a small JSON file at `<workspace>/state/cores/<hostname>.alive` every
30 seconds while running. The file's content reports the core's pid, host,
start time, last beat, and a free-form status string; the file's mtime is
the cross-host "is this core still up?" signal.

Why
---
Today's "is the core alive?" check reads `core-status.json` at the workspace
root — a single file written by the proactive-loop each pass. That's fine
for a single-machine install: one core, one status. The moment we want
multi-core (multiple Claude Code sessions sharing a workspace, or sutando
running on both Mac Studio + MacBook against a synced workspace), one file
can no longer represent N processes.

Per-host file at `state/cores/<hostname>.alive`:
  • Each running core writes only its own file (no contention).
  • Any process can read the directory to see who's alive across the fleet.
  • mtime is the authoritative liveness signal (younger than ~90s = alive).
  • Future lease-based scheduler consumes this to know who can pick up work.

This script is intentionally tiny and standalone — startup.sh launches it as
a background process. SIGTERM/SIGINT clean up the .alive file so a graceful
shutdown is visible immediately (vs. waiting for mtime-staleness timeout).

Usage:
  python3 src/core_heartbeat.py                  # default 30s interval
  python3 src/core_heartbeat.py --interval 10    # for tests
  python3 src/core_heartbeat.py --status busy    # set the status string

Runs forever until killed. Exit codes:
  0 — clean shutdown (SIGTERM/SIGINT received)
  Other — fatal write error (unrecoverable; supervisor should restart)
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

# Resolve workspace via the M0 helper (PR #1395 / v0.8 #1440) — the previous
# inlined env-or-legacy-default resolution wrote .alive files where no
# post-M0 reader looks (health-check + dashboard read resolve_workspace()/
# state/cores/), so every core reported dead. workspace_default is a sibling
# module (stdlib-only deps), so the old "dep-free" rationale no longer buys
# anything. Fail loud on import error: a heartbeat written to the wrong tree
# is worse than no heartbeat (supervisor restarts on crash; see module header).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()

CORES_DIR = WORKSPACE / "state" / "cores"


def _hostname() -> str:
    """Per-host label for the `.alive` filename. Delegates to
    `util_paths._host_label()` — the single source of truth (honors
    `$SUTANDO_HOST_LABEL`, else short hostname) — so the heartbeat label stays
    in lockstep with the `hosts/<host>/` per-host dir and survives DHCP
    hostname drift (a node whose `hostname` is a DHCP/Comcast name that flaps
    would otherwise write two divergent `<label>.alive` files). Falls back to
    the raw short hostname if util_paths is unavailable."""
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:
        return socket.gethostname().split(".")[0]


def _alive_path() -> Path:
    return CORES_DIR / f"{_hostname()}.alive"


def _locality() -> dict[str, str]:
    """The core's locality — self-reported (Track 10, owner 2026-07-10).

    `kind`: ``local`` when this core runs on one of the owner's own machines
    (a normal ``startup.sh`` launch), ``cloud`` when spawned by the hosted
    spawn-user-core template. The template sets ``$SUTANDO_CORE_LOCALITY=cloud``;
    an absent or unrecognized value defaults to ``local`` (a hand-started core
    is local by construction — fail toward the safe, common case). ``host`` is
    the per-host label, so a client can render WHICH machine ("MacBook Pro
    (yours)" vs "mac-mini (yours, remote)").

    Consumed downstream by the broker presence sweep → ``space.ag2.presence`` →
    a client locality badge (the remaining two Track-10 slices). Self-reported
    v1; attestation is a Track 2/4 tie-in. Same runtime-authored-state pattern
    as ``socket`` above — the answer lives in the core's own environment.
    """
    kind = os.environ.get("SUTANDO_CORE_LOCALITY", "local").strip().lower()
    if kind not in ("local", "cloud"):
        kind = "local"
    return {"kind": kind, "host": _hostname()}


def write_beat(status: str = "running") -> None:
    """Write one heartbeat record. Atomic-via-tmp-then-rename so a concurrent
    reader never sees a partial file."""
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    target = _alive_path()
    payload = {
        "host": _hostname(),
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "last_beat_at": time.time(),
        "status": status,
        # The tmux socket THIS core actually runs on. Recorded here — in the
        # core's own environment — so it is the authoritative, runtime-authored
        # answer to "which socket?" for readers that cannot reconstruct the
        # launch env (e.g. `sutando-config.sh runtime` invoked by the desktop
        # app, whose ambient SUTANDO_TMUX_SOCKET points at a *different* bundled
        # socket). Mirrors start-cli.sh's resolution exactly.
        "socket": os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock"),
        # Self-reported locality (Track 10): {kind: local|cloud, host}. Additive
        # and informational — mtime remains the liveness signal — so readers
        # that don't know the field are unaffected.
        "locality": _locality(),
        "schema_version": 2,
    }
    # Per-PROCESS staging file: with a shared name, two concurrent first beats
    # destroy each other mid-`replace` (one writer's rename removes the other's
    # staging file → FileNotFoundError, and interleaved writes can leave the
    # final .alive as invalid JSON — both reproduced in the #2201 review).
    tmp = target.parent / f"{target.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace


_STARTED_AT: float = time.time()
_SHUTDOWN_REQUESTED = False


def another_heartbeat_alive(staleness_s: float = 90.0) -> "int | None":
    """Self-guard: return the pid of an ALREADY-BEATING heartbeat for this
    host, or None if this process should take over.

    "Already beating" requires ALL of: the .alive file exists, its mtime is
    younger than `staleness_s` (the documented cross-host liveness threshold),
    its payload names a pid, that pid is a live process, and it isn't us.
    Anything else — missing/stale file, malformed payload, dead pid — means
    the previous owner is gone and we take over (same recoverability stance
    as the mtime-staleness readers).

    Why: two concurrent heartbeats write the same .alive and flap it between
    pids — harmless for mtime-liveness, but ambiguous for consumers that use
    the pid as a control target (the pause/stop-core path, #2198). With this
    guard a double-start (e.g. startup.sh + the schedule-crons step-5.5
    backstop landing in the same window, #2199) resolves to exactly one
    writer, deterministically.
    """
    target = _alive_path()
    try:
        st = target.stat()
        if time.time() - st.st_mtime > staleness_s:
            return None
        pid = int(json.loads(target.read_text())["pid"])
    except Exception:
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # ESRCH — no such process; take over.
    except PermissionError:
        # EPERM — the process EXISTS but we may not signal it (sandboxed or
        # privilege-separated launcher). That is a live heartbeat: yield.
        return pid
    except OSError:
        return None  # anything else — treat as dead; staleness readers recover.
    return pid


_LOCK_FD: "int | None" = None


def try_acquire_ownership() -> "int | None":
    """Atomically claim single-writer ownership for this host, or name who to
    yield to. Returns None on success; a pid (or -1 when the holder is not yet
    identifiable) when another starter/beater owns the host.

    Check-then-act on the .alive file alone is NOT enough: on a true
    simultaneous start every process can observe "no fresh owner" before any
    first beat lands (reproduced with a 5-way forked barrier in the #2201
    review — all five proceeded as owners). The claim must be atomic, so it
    rides an flock(LOCK_EX | LOCK_NB) on `<host>.lock`, held open for the
    process lifetime and released by the kernel on ANY death — no stale-lock
    recovery needed, which is why it beats O_EXCL lock files here.

    The freshness/pid guard still runs AFTER the flock: a beater from a
    pre-flock build (or a foreign writer) doesn't hold the lock but is still
    a legitimate owner — yield to it rather than fight over the file.
    """
    global _LOCK_FD
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = CORES_DIR / f"{_hostname()}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        # The holder's pid is only knowable once it has beaten.
        try:
            return int(json.loads(_alive_path().read_text())["pid"])
        except Exception:
            return -1
    other = another_heartbeat_alive()
    if other is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return other
    _LOCK_FD = fd  # deliberately held (and leaked) for the process lifetime
    return None


def _handle_signal(signum: int, frame) -> None:
    """Mark shutdown so the loop exits at the top of the next sleep; also
    unlink the .alive file so peers see this core leave immediately rather
    than wait for mtime staleness."""
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True
    try:
        _alive_path().unlink(missing_ok=True)
    except Exception:  # pragma: no cover — best-effort cleanup
        pass


def run_forever(interval: float = 30.0, status: str = "running") -> int:
    """Heartbeat loop. Returns the exit code (0 on graceful shutdown)."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while not _SHUTDOWN_REQUESTED:
        # Re-check ownership before every beat: if a DIFFERENT live pid now
        # owns the file (e.g. this process wedged long enough to be declared
        # stale and something else legitimately took over), exit instead of
        # flapping the file back (#2201 review hardening).
        usurper = another_heartbeat_alive()
        if usurper is not None:
            print(f"core_heartbeat: pid {usurper} took over the host heartbeat — "
                  "exiting (yield instead of pid-flapping)", flush=True)
            return 0
        try:
            write_beat(status=status)
        except Exception as e:
            # Don't die on transient FS hiccups — log + retry next tick.
            print(f"core_heartbeat: write failed: {e}", file=sys.stderr, flush=True)
        # Sleep in small slices so SIGTERM is responsive (signal handler
        # sets the flag; we check it between slices instead of blocking
        # for the full `interval`).
        slept = 0.0
        slice_s = min(1.0, interval)
        while slept < interval and not _SHUTDOWN_REQUESTED:
            time.sleep(slice_s)
            slept += slice_s
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval", type=float, default=30.0, help="seconds between beats (default: 30)")
    p.add_argument("--status", type=str, default="running", help="status string written into the .alive file")
    p.add_argument("--once", action="store_true", help="write a single beat and exit (for tests/debugging)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.once:
        # Debug/test escape hatch: --once is a forced single beat, exempt from
        # the self-guard on purpose.
        write_beat(status=args.status)
        return 0
    other = try_acquire_ownership()
    if other is not None:
        who = f"pid {other}" if other > 0 else "another starter (lock held, no beat yet)"
        print(f"core_heartbeat: {who} already owns this host's heartbeat — "
              "exiting (self-guard; the desired single-writer state already holds)",
              flush=True)
        return 0
    # Anonymous, opt-out product telemetry: one event per real core boot so
    # maintainers can count active installs (OSS + desktop). No-op when opted
    # out or no key is configured. Never blocks; see src/telemetry.py + TELEMETRY.md.
    try:  # pragma: no cover — fire-and-forget glue; telemetry logic tested in tests/telemetry.test.py
        from telemetry import capture  # sibling module (src/ already on sys.path)

        capture("core_started", {"interval_s": args.interval})
    except Exception:  # pragma: no cover — telemetry must never break the core
        pass
    return run_forever(interval=args.interval, status=args.status)


if __name__ == "__main__":
    sys.exit(main())

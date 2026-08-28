#!/usr/bin/env python3
"""Production lead driver (L3): binds PoolLead to the live workspace.

Followers = state/cores/<inst>.alive files matching the pool prefix; alive =
mtime within the shared pool heartbeat bounds.
The lead stamps its own `pool-lead.alive` each sweep so followers can detect
lead loss and degrade (pool_follower.lead_alive reads it).

Usage: python3 scripts/pool-lead-daemon.py [--interval 2.0] [--prefix core-]
Stop with SIGTERM/SIGINT; the beat file is unlinked on exit so followers
degrade immediately instead of waiting out the stale window.
"""
from __future__ import annotations

# flake8: noqa: E402 — imports follow the sys.path bootstrap

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE.parent / "src" / "runtime-api"))

from pool_follower import (HEARTBEAT_FUTURE_TOLERANCE_S, LEAD_LABEL,
                           LEAD_STALE_S)
from pool_lead import PoolLead
from pool_metrics import PoolMetrics
from pool_status import PoolStatusWriter



def _workspace() -> Path:
    out = subprocess.run(
        ["bash", str(_HERE / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, timeout=10)
    return Path(out.stdout.strip())


def _heartbeat_alive(cores: Path, instance: str, now_fn=time.time) -> bool:
    try:
        age = now_fn() - (cores / f"{instance}.alive").stat().st_mtime
    except OSError:
        return False
    return -HEARTBEAT_FUTURE_TOLERANCE_S <= age < LEAD_STALE_S


def acquire_singleton(cores: Path):
    """Exclusive for this daemon's lifetime, or None when a lead already holds it.

    The boundary lives here, not in the wrapper: a pgrep-then-exec check lets two
    starts pass simultaneously, and startup.sh can launch a lead without the
    wrapper at all. KEEP THE RETURNED HANDLE OPEN — flock is tied to the open file
    description, so closing it releases the lock while the daemon still runs."""
    cores.mkdir(parents=True, exist_ok=True)
    handle = open(cores / f"{LEAD_LABEL}.lock", "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_beat(beat: Path, pid: int) -> bool:
    """Remove the heartbeat only if it still names `pid`.

    An unconditional unlink lets a losing or exiting instance delete the LIVE
    lead's beat, and every follower then degrades to leaderless claiming."""
    try:
        record = json.loads(beat.read_text())
    except (OSError, ValueError):
        return False
    if record.get("pid") != pid:
        return False
    try:
        beat.unlink()
    except OSError:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--prefix", default="core-",
                    help="instance-id prefix that marks pool followers")
    a = ap.parse_args()
    ws = _workspace()
    tasks, state = ws / "tasks", ws / "state"
    cores = state / "cores"

    def followers():
        try:
            return [f.stem for f in cores.glob(f"{a.prefix}*.alive")]
        except OSError:
            return []

    def alive(inst: str) -> bool:
        return _heartbeat_alive(cores, inst)

    lock = acquire_singleton(cores)
    if lock is None:
        print("pool-lead: another lead holds the singleton lock — standing down",
              flush=True)
        return 0

    lead = PoolLead(tasks, state, followers, alive,
                    metrics=PoolMetrics(state))
    status = PoolStatusWriter(tasks, state, followers, alive)
    beat = cores / f"{LEAD_LABEL}.alive"
    running = {"on": True}

    def _stop(_sig, _frm):
        running["on"] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"pool-lead: ws={ws} interval={a.interval}s prefix={a.prefix!r}",
          flush=True)
    while running["on"]:
        cores.mkdir(parents=True, exist_ok=True)
        beat.write_text(json.dumps(
            {"role": "pool-lead", "pid": os.getpid(), "ts": time.time()}))
        for name, inst in lead.sweep():
            print(f"assigned {name} -> {inst}", flush=True)
        for name in lead.reclaim_dead():
            print(f"reclaimed {name}", flush=True)
        for name, disposition in lead.reclaim_claimed():
            print(f"reclaimed-claim {name} -> {disposition}", flush=True)
        status.maybe_write()
        time.sleep(a.interval)
    release_beat(beat, os.getpid())  # degrade followers NOW, not after the stale window
    print("pool-lead: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

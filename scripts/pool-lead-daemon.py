#!/usr/bin/env python3
"""Production lead driver (L3): binds PoolLead to the live workspace.

Followers = state/cores/<inst>.alive files matching the pool prefix; alive =
mtime within LEAD_STALE_S bounds (future-dated = dead, same rule everywhere).
The lead stamps its own `pool-lead.alive` each sweep so followers can detect
lead loss and degrade (pool_follower.lead_alive reads it).

Usage: python3 scripts/pool-lead-daemon.py [--interval 2.0] [--prefix core-]
Stop with SIGTERM/SIGINT; the beat file is unlinked on exit so followers
degrade immediately instead of waiting out the stale window.
"""
from __future__ import annotations

# flake8: noqa: E402 — imports follow the sys.path bootstrap

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE.parent / "src" / "runtime-api"))

from pool_follower import LEAD_STALE_S
from pool_lead import PoolLead
from pool_metrics import PoolMetrics
from pool_notify import PoolNotifier
from pool_scale import ScaleLedger, decide as scale_decide, observe as scale_observe
from pool_status import PoolStatusWriter

LEAD_LABEL = "pool-lead"

# The daemon is the composition root: it binds the notify transport (a skill
# script) so the policy module stays free of any concrete skill path.
_NOTIFY_SCRIPT = _HERE.parent / "skills" / "task-progress" / "scripts" / "notify.py"
_KICK_SCRIPT = _HERE / "kick-pool.sh"
# launchd PENDS non-demand spawns (both KeepAlive and StartInterval), so the
# plist never revives a dead follower — the lead drives recovery instead.
RECOVERY_EVERY_S = 60


def _run_recovery() -> str:
    try:
        r = subprocess.run(["bash", str(_KICK_SCRIPT)],
                           capture_output=True, text=True, timeout=60)
        return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"recovery sweep failed: {e}"


def _send_notice(source: str, channel: str, message: str) -> bool:
    chan_flag = "--chat-id" if source == "telegram" else "--channel-id"
    try:
        r = subprocess.run(
            [sys.executable, str(_NOTIFY_SCRIPT), "--source", source,
             chan_flag, channel, "--message", message],
            capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _workspace() -> Path:
    out = subprocess.run(
        ["bash", str(_HERE / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, timeout=10)
    return Path(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--prefix", default="core-",
                    help="instance-id prefix that marks pool followers")
    ap.add_argument("--pool-max", type=int,
                    default=int(os.environ.get("SUTANDO_POOL_MAX", "3")),
                    help="autoscale cap; 0 disables scale-up (owner rule: "
                         "grow when every core is saturated and work queues)")
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
        try:
            age = time.time() - (cores / f"{inst}.alive").stat().st_mtime
        except OSError:
            return False
        return 0 <= age < LEAD_STALE_S

    def runtime_of(inst: str) -> str:
        # The core's own plist is the only authority; unreadable or unstated
        # means claude, matching every plist written before the runtime flag.
        plist = (Path.home() / "Library/LaunchAgents"
                 / f"com.sutando.{inst}.plist")
        try:
            body = plist.read_text(errors="replace")
        except OSError:
            return "claude"
        m = re.search(r"<key>POOL_RUNTIME</key>\s*<string>([^<]*)</string>",
                      body)
        rt = (m.group(1).strip() if m else "") or "claude"
        return rt if rt in ("claude", "codex") else "claude"

    lead = PoolLead(tasks, state, followers, alive,
                    metrics=PoolMetrics(state), runtime_fn=runtime_of)
    status = PoolStatusWriter(tasks, state, followers, alive)
    notifier = PoolNotifier(tasks, state, _send_notice)
    ledger = ScaleLedger(state)
    last_prune = 0.0
    last_recovery = 0.0
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
            if notifier.on_assigned(name, inst):
                print(f"notified-handoff {name}", flush=True)
        for name in lead.reclaim_dead():
            print(f"reclaimed {name}", flush=True)
        for name, disposition in lead.reclaim_claimed():
            print(f"reclaimed-claim {name} -> {disposition}", flush=True)
        for name in lead.reclaim_stuck_assignments():
            print(f"reclaimed-stuck {name}", flush=True)
        if time.time() - last_prune > 3600:
            n = lead.prune_done_flags()
            if n:
                print(f"pruned {n} stale done-flag(s)", flush=True)
            last_prune = time.time()
        for stem in notifier.check_stalls():
            print(f"notified-stall {stem}", flush=True)
        if time.time() - last_recovery > RECOVERY_EVERY_S:
            out = _run_recovery()
            acted = [ln for ln in out.splitlines()
                     if "NO SESSION" in ln or "kickstart" in ln or "staged" in ln]
            for line in acted:
                print(f"recovery: {line}", flush=True)
            # Always emit, even when idle: a sweep that is silent while healthy
            # is indistinguishable from a sweep that stopped running.
            if not acted:
                live = sum(1 for ln in out.splitlines() if ln.startswith("core-"))
                print(f"recovery: ok ({live} session(s) healthy)", flush=True)
            last_recovery = time.time()
        # Autoscale, scale-UP only: shrinking can strand a core's live claims,
        # so it stays a manual operation (--pool N) for now.
        if a.pool_max > 0:
            live = followers()
            pending, in_flight = scale_observe(tasks, live)
            led = ledger.load()
            if pending or any(in_flight.values()):
                ledger.record(busy=True)
            new_n = scale_decide(pending, in_flight, len(live),
                                 min_n=1, max_n=a.pool_max,
                                 last_change_ts=led["last_change_ts"],
                                 last_busy_ts=led["last_busy_ts"],
                                 now=time.time())
            if new_n is not None and new_n > len(live):
                r = subprocess.run(
                    ["bash", str(_HERE / "install-core-pool.sh"), str(new_n)],
                    capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    ledger.record(changed=True)
                    print(f"scaled-up pool {len(live)} -> {new_n}", flush=True)
                else:
                    print(f"scale-up failed rc={r.returncode}: "
                          f"{(r.stderr or '').strip()[:200]}", flush=True)
        status.maybe_write()
        time.sleep(a.interval)
    try:
        beat.unlink()  # followers degrade NOW, not after the stale window
    except OSError:
        pass
    print("pool-lead: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

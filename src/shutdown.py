#!/usr/bin/env python3
"""Graceful-shutdown sentinel — a durable, cross-process "we are shutting down
on purpose (not crashing)" signal.

Motivation (owner ask 2026-07-17): the core has partial shutdown handling —
core_heartbeat unlinks its .alive file on SIGTERM, voice-agent traps signals,
restart.sh drains-and-waits. What's missing is a signal the CORE agent loop can
check to *finish the current task and exit cleanly* rather than be killed
mid-pass and leave an orphaned task recovered only after the result-watcher
timeout.

This sentinel is that signal on the STOP paths only. Writers (restart.sh
--stop-only, an explicit "stop") call mark_shutdown(); the core launchers clear it on
boot; readers (the proactive loop at the top of a pass, bridges) call
is_shutting_down(). A plain restart marks and clears it within seconds and the
core is meant to survive, so clean core exit is a --stop-only guarantee; what
holds on BOTH paths is the watcher's intake gate. It lives under state/ next to the other liveness
files and carries a reason + timestamp so health-check can distinguish a
graceful stop from a crash.

TWO gates, because a host runs more than one Sutando on one workspace. The
workspace-wide gate (`state/shutdown.sentinel`) is what `--scope all` marks, and
every watcher consults it. The INSTANCE gate sits beside the instance's own
watcher record, keyed exactly like it (src/util_paths.py owns both paths), and
is what the default `--scope core` marks: a core restart must not hold a peer
worker's intake. A watcher consults its own instance gate AND the shared one;
`is_shutting_down()` reads both for this process's identity.

CLI:
  python3 src/shutdown.py mark [reason] [--gate workspace|instance] [--state-dir D]
  python3 src/shutdown.py clear [--gate workspace|instance] [--state-dir D]
                                          # default: BOTH of this identity's gates (startup)
  python3 src/shutdown.py check [--state-dir D]   # exit 0 if either gate is set
  python3 src/shutdown.py path [--gate workspace|instance] [--state-dir D]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402
from util_paths import instance_shutdown_gate_path, shutdown_gate_path  # noqa: E402

GATES = ("workspace", "instance")


def _sentinel_path() -> Path:
    """The workspace-wide gate. Tests redirect this ONE resolver; the instance
    gate derives its state dir from it so both land in the same directory."""
    return shutdown_gate_path(resolve_workspace() / "state")


def _gate_path(gate: str = "workspace", state_dir=None, instance=None) -> Path:
    if gate not in GATES:
        raise ValueError(f"unknown gate: {gate!r} (workspace|instance)")
    if gate == "workspace":
        return shutdown_gate_path(state_dir) if state_dir is not None else _sentinel_path()
    base = Path(state_dir) if state_dir is not None else _sentinel_path().parent
    return instance_shutdown_gate_path(base, instance=instance)


def _instance_gate_or_none(state_dir=None) -> "Path | None":
    """This process's instance gate, or None when its identity cannot be
    resolved: a reader must still answer from the workspace-wide gate."""
    try:
        return _gate_path("instance", state_dir)
    except Exception:  # noqa: BLE001 — an absent runtime-api is not a shutdown
        return None


def mark_shutdown(reason: str = "manual", gate: str = "workspace",
                  state_dir=None, instance=None) -> Path:
    """Write the shutdown sentinel. Idempotent — overwrites any prior one."""
    p = _gate_path(gate, state_dir, instance)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reason": reason, "ts": int(time.time())}) + "\n")
    return p


def clear_shutdown(gate=None, state_dir=None, instance=None) -> None:
    """Remove the sentinel (called on boot). Never raises if it's absent.
    `gate=None` clears BOTH of this identity's gates: a launcher booting a core
    after a core-scope stop must lift the instance gate that stop left set."""
    targets = [_gate_path(gate, state_dir, instance)] if gate else [_gate_path("workspace", state_dir)]
    if gate is None:
        inst = _instance_gate_or_none(state_dir)
        if inst is not None:
            targets.append(inst)
    for p in targets:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _present_gates(state_dir=None) -> "list[Path]":
    found = []
    ws = _gate_path("workspace", state_dir)
    if ws.exists():
        found.append(ws)
    inst = _instance_gate_or_none(state_dir)
    if inst is not None and inst.exists():
        found.append(inst)
    return found


def is_shutting_down(state_dir=None) -> bool:
    """True if a shutdown sentinel is present — the workspace-wide gate or this
    identity's own instance gate. Cheap enough to call each pass."""
    return bool(_present_gates(state_dir))


def shutdown_info(state_dir=None) -> dict | None:
    """The sentinel's {reason, ts}, or None if not shutting down / unreadable."""
    present = _present_gates(state_dir)
    if not present:
        return None
    try:
        return json.loads(present[0].read_text())
    except (OSError, ValueError):
        return {"reason": "unknown", "ts": 0}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=os.path.basename(argv[0]) if argv else "shutdown.py",
                                 add_help=False)
    ap.add_argument("cmd", nargs="?", default="check")
    ap.add_argument("reason", nargs="?", default=None)
    ap.add_argument("--gate", choices=GATES, default=None)
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--instance", default=None)
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit:
        return 2
    if args.cmd == "mark":
        print(mark_shutdown(args.reason or "manual", args.gate or "workspace",
                            args.state_dir, args.instance))
        return 0
    if args.cmd == "clear":
        clear_shutdown(args.gate, args.state_dir, args.instance)
        return 0
    if args.cmd == "check":
        return 0 if is_shutting_down(args.state_dir) else 1
    if args.cmd == "path":
        # Shell launchers stash/restore the sentinel byte-for-byte around a
        # launch; they must not re-derive this path and drift from it.
        print(_gate_path(args.gate or "workspace", args.state_dir, args.instance))
        return 0
    print(f"usage: {argv[0]} mark|clear|check|path [reason] [--gate workspace|instance] "
          f"[--state-dir D] [--instance I]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

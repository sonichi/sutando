#!/usr/bin/env python3
"""The core does not speak in a worker-bound room while that worker is alive.

Owner directive (2026-09-18): a room bound to a worker is the worker's voice;
the core opens its mouth there only when the worker's liveness says dead and
recovery is what is speaking. Recent-activity files say where the owner WAS,
never whose room it is — the resolver must read state/bindings.json.

Run: python3 tests/owner-channel-refuses-bound-rooms.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from owner_channel import bound_worker, may_core_speak, worker_alive  # noqa: E402

FAILED: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        FAILED.append(what)


ROOM = "!bound:ag2.space"
OTHER = "!free:ag2.space"
WID = "39041ce6a4444e28bf94859ec2426270"


def ws_with(bindings, watcher_pid=None):
    ws = Path(tempfile.mkdtemp())
    (ws / "state").mkdir()
    if bindings is not None:
        (ws / "state" / "bindings.json").write_text(json.dumps({"bindings": bindings}))
    if watcher_pid is not None:
        (ws / "state" / f"watch-tasks-stream-{WID}.pid").write_text(f"{watcher_pid}\n")
    (ws / "state" / "last-owner-activity.json").write_text(
        json.dumps({"channel": "ag2space", "channel_id": ROOM, "ts": 0}))
    return ws


print("bindings and liveness")
ws = ws_with({ROOM: WID}, watcher_pid=os.getpid())
check(bound_worker(ws, ROOM) == WID, "a bound room names its worker")
check(bound_worker(ws, OTHER) is None, "an unbound room names nobody")
check(worker_alive(ws, WID), "a sentinel naming a live pid is alive (positive control)")
ok, why = may_core_speak(ws, ROOM)
check(not ok and "alive" in why, f"bound + alive → REFUSE ({why})")
ok, why = may_core_speak(ws, OTHER)
check(ok and why == "unbound", "unbound room → allow")

dead = ws_with({ROOM: WID}, watcher_pid=2**22 - 7)  # no such pid
ok, why = may_core_speak(dead, ROOM)
check(ok and "DEAD" in why, f"bound + dead worker → allow, reason names recovery ({why})")
nosent = ws_with({ROOM: WID})
ok, _ = may_core_speak(nosent, ROOM)
check(ok, "bound + no watcher sentinel at all → worker is dead → allow")

nofile = ws_with(None)
check(may_core_speak(nofile, ROOM)[0], "absent bindings.json → nothing is bound → allow")
bad = ws_with({ROOM: WID})
(bad / "state" / "bindings.json").write_text("{not json")
try:
    may_core_speak(bad, ROOM)
    check(False, "unreadable bindings must not read as unbound")
except ValueError:
    check(True, "unreadable bindings raise (the caller stays silent)")

print("the CLI the loop step calls")
r = subprocess.run([sys.executable, str(ROOT / "src" / "owner_channel.py"), "--workspace", str(ws)],
                   capture_output=True, text=True)
check(r.returncode == 3 and r.stdout.startswith("refuse " + ROOM), f"live worker's room → exit 3 'refuse' ({r.stdout.strip()})")
r = subprocess.run([sys.executable, str(ROOT / "src" / "owner_channel.py"), "--workspace", str(dead)],
                   capture_output=True, text=True)
check(r.returncode == 0 and r.stdout.startswith("allow " + ROOM), f"dead worker's room → exit 0 'allow' ({r.stdout.strip()})")

print("the supervisor relay resolves its target through the same rule")
sys.path.insert(0, str(ROOT / "src"))
import importlib.util
spec = importlib.util.spec_from_file_location("csr", ROOT / "src" / "core-supervisor-relay.py")
csr = importlib.util.module_from_spec(spec); spec.loader.exec_module(csr)
src, ch = csr.resolve_active_target(str(ws / "state" / "last-owner-activity.json"))
check((src, ch) == ("", ""), "relay: a live worker's room resolves to no target (macOS-only)")
src, ch = csr.resolve_active_target(str(dead / "state" / "last-owner-activity.json"))
check(ch == ROOM, "relay: a dead worker's room still resolves (recovery may speak)")
src, ch = csr.resolve_active_target(str(bad / "state" / "last-owner-activity.json"))
check((src, ch) == ("", ""), "relay: unreadable bindings → no target, never a guess")

print(f"\n{'FAILED: ' + '; '.join(FAILED) if FAILED else 'all checks passed'}")
sys.exit(1 if FAILED else 0)

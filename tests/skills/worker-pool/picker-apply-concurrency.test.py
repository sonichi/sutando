#!/usr/bin/env python3
"""Two concurrent picker applies cannot leave the ledger and the roster disagreeing.

The gate, the sequence record and the roster mutation used to be separate
critical sections, so an old pin could record sequence 1, be paused, watch a
newer unpin record 2 and unbind, then resume and re-bind. The durable ledger
then said the unpin was newest while the roster -- and the advertisement built
from it -- carried the pin the owner had displaced.

Duplicate watchers are an explicitly handled state and picker mutation happens
during the probe, before task claiming, so two concurrent applies are reachable
rather than theoretical.

This drives the production `apply()` from two real processes and pauses the
first one INSIDE its critical section, which is the interleaving under test.

Run: python3 tests/skills/worker-pool/picker-apply-concurrency.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
W = "a" * 32
ROOM = "!race:example.test"
FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def workspace() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="picker-race-"))
    (ws / "state").mkdir(parents=True)
    (ws / "results").mkdir(parents=True)
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"version": 1, "workers": {W: {"state": "live"}}, "bindings": {}}))
    return ws


DRIVER = '''
import json, sys, time
sys.path.insert(0, {scripts!r})
sys.path.insert(0, {src!r})
import worker_picker_commands as wpc
ws, action, task_id, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])

# The pause lands BETWEEN the record and the roster mutation: patching the record
# itself puts the sleep inside its lock, which cannot interleave by construction.
if delay:
    for _name in ("bind_room", "unbind_room"):
        _real = getattr(wpc.pr, _name)
        def _slow(*a, __r=_real, **k):
            time.sleep(delay)
            return __r(*a, **k)
        setattr(wpc.pr, _name, _slow)

cmd = ({{"action": "pin", "room": {room!r}, "workers": [{worker!r}], "dedicated": False}}
       if action == "pin" else {{"action": "unpin", "room": {room!r}}})
out = wpc.apply(ws, cmd, task_id=task_id)
print(json.dumps(out or {{}}))
'''.format(scripts=str(SCRIPTS), src=str(REPO / "src"), room=ROOM, worker=W)


def run(ws, action, task_id, delay, wait=True):
    p = subprocess.Popen([sys.executable, "-c", DRIVER, str(ws), action, task_id, str(delay)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if wait:
        p.wait(timeout=60)
    return p


def state(ws):
    roster = json.loads((ws / "state" / "roster.json").read_text())
    try:
        log = json.loads((ws / "state" / "picker-applied.json").read_text())
    except Exception:
        log = {}
    return roster.get("bindings", {}).get(ROOM), (log.get("rooms") or {}).get(ROOM)


# --- the interleaving -------------------------------------------------------
ws = workspace()
old = run(ws, "pin", "task-old", delay=2.0, wait=False)   # records, then pauses
time.sleep(0.6)
new = run(ws, "unpin", "task-new", delay=0.0)             # must WAIT for the lock
old.wait(timeout=60)
binding, room_rec = state(ws)

print(f"    roster binding for the room : {binding!r}")
print(f"    ledger's newest for the room: {room_rec}")
print()

# Whichever order the lock grants, the LAST writer's intent is what must be on disk.
last_action = (room_rec or {}).get("action")
agree = (last_action == "unpin" and binding is None) or (last_action == "pin" and binding == W)
check("the ledger and the roster agree on the newest command", agree,
      f"ledger says {last_action!r} but the roster holds {binding!r}")

# --- controls ---------------------------------------------------------------
ws2 = workspace()
run(ws2, "pin", "task-p", delay=0.0)
b2, _ = state(ws2)
check("control: an uncontended pin binds the room", b2 == W, f"binding={b2!r}")

run(ws2, "unpin", "task-u", delay=0.0)
b3, rec3 = state(ws2)
check("control: a following unpin clears it and is recorded newest",
      b3 is None and (rec3 or {}).get("action") == "unpin",
      f"binding={b3!r} rec={rec3}")

print(("FAILED — " + ", ".join(FAILURES)) if FAILURES
      else "PASS — the gate, the record and the mutation are one transaction")
sys.exit(1 if FAILURES else 0)

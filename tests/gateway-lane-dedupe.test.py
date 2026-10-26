#!/usr/bin/env python3
"""Cross-lane duplicate guard: one room message seen by two lanes becomes one task.

Covers src/lane_dedupe.py and the gateway bridge's delegation to it:
  1. the same event on two lanes queues one task; the second lane answers its own
     task id with the redelivery control result, so the broker settles it;
  2. the same lane seeing the message again keeps today's behaviour;
  3. a task without source_message_id, or a hook task, is never deduped;
  4. claims expire after the TTL;
  5. an unwritable or malformed store fails open, with one warning line;
  6. processes racing on one key: exactly one wins (real multiprocessing);
  7. the loader installs the gate for a connection lane and never for a seat.

Run: python3 tests/gateway-lane-dedupe.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import multiprocessing
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ, isolated_child_env  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import lane_dedupe  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _race_worker(state_dir, key, lane, barrier, out):
    barrier.wait()
    out.put((lane, lane_dedupe.claim(Path(state_dir), key, lane, log=lambda _m: None)))


def main():
    rgb = _load("remote_gateway_bridge", REPO / "src" / "remote-gateway-bridge.py")
    tmp = Path(tempfile.mkdtemp(prefix="rgb-lane-dedupe-test-"))
    rgb.WS = tmp
    rgb.TASKS_DIR = tmp / "tasks"
    rgb.RESULTS_DIR = tmp / "results"
    rgb.ARCHIVE_RESULTS_DIR = tmp / "results" / "archive"
    logs = []
    rgb._log = logs.append

    def on_lane(lane, broker_id, hold=False, **fields):
        rgb.GATEWAY_INSTANCE = lane
        try:
            return rgb._write_task({"id": broker_id, "task": "hello", **fields}, hold=hold)
        finally:
            rgb.GATEWAY_INSTANCE = ""

    room = {"channel_id": "!room:example.org", "source_room_id": "!room:example.org"}

    # 7a. Delegation: a connection lane's loader installs the module's gate.
    check("the loader installs the lane dedupe gate", rgb.TASK_INTAKE_GATE is rgb._lane_dedupe_gate)

    # 1. Same event, two lanes.
    first = on_lane("", "task-A1", source_message_id="$ev1", **room)
    second = on_lane("sspace", "task-B1", source_message_id="$ev1", **room)
    check("the first lane queues the task", first == ("task-A1", True)
          and (rgb.TASKS_DIR / "task-A1.txt").exists(), repr(first))
    check("the second lane queues nothing", not (rgb.TASKS_DIR / "task-sspace~task-B1.txt").exists())
    settled = rgb.RESULTS_DIR / "task-sspace~task-B1.txt"
    check("the second lane answers with the redelivery control result",
          settled.exists() and settled.read_text() == rgb.GATEWAY_REDELIVERY_RESULT)
    check("its ack is durable and its control result is this process's own",
          second == ("task-sspace~task-B1", True) and "task-sspace~task-B1" in rgb._REDELIVERED, repr(second))
    named = [m for m in logs if "lane dedupe" in m]
    check("one log line names both lanes", len(named) == 1 and "lane sspace" in named[0]
          and "lane primary" in named[0], repr(named))

    # 1b. The same event on the second lane through the ack_first path is settled too.
    held = on_lane("sspace", "task-B1h", hold=True, source_message_id="$ev1", **room)
    check("an ack_first copy on the other lane is settled, not staged",
          held is not None and (rgb.RESULTS_DIR / "task-sspace~task-B1h.txt").exists())

    # 2. The same lane again: a redelivery of the queued task, and a second broker task.
    again = on_lane("", "task-A1", source_message_id="$ev1", **room)
    check("a same-lane redelivery of the queued task is unchanged",
          again is not None and again[0] == "task-A1" and not (rgb.RESULTS_DIR / "task-A1.txt").exists())
    other = on_lane("", "task-A2", source_message_id="$ev1", **room)
    check("the same lane is never deduped against itself",
          other == ("task-A2", True) and (rgb.TASKS_DIR / "task-A2.txt").exists())

    # 3. No message id, or a hook task: never deduped.
    on_lane("", "task-N1", **room)
    on_lane("sspace", "task-N2", **room)
    check("tasks without source_message_id are delivered on both lanes",
          (rgb.TASKS_DIR / "task-N1.txt").exists() and (rgb.TASKS_DIR / "task-sspace~task-N2.txt").exists())
    on_lane("", "task-H1", source_message_id="$ev2", hook={"id": "h"}, **room)
    on_lane("sspace", "task-H2", source_message_id="$ev2", hook={"id": "h"}, **room)
    check("hook tasks are delivered on both lanes",
          (rgb.TASKS_DIR / "task-H1.txt").exists() and (rgb.TASKS_DIR / "task-sspace~task-H2.txt").exists())
    check("the key falls back to channel_id and is absent without a message id",
          lane_dedupe.key_of({"channel_id": "!r", "source_message_id": "$x"})
          == lane_dedupe.key_of({"source_room_id": "!r", "source_message_id": "$x"})
          and lane_dedupe.key_of({"channel_id": "!r"}) is None)

    # A gate that raises delivers the task.
    real_gate = rgb.TASK_INTAKE_GATE

    def boom(_task):
        raise RuntimeError("store exploded")
    rgb.TASK_INTAKE_GATE = boom
    on_lane("sspace", "task-G1", source_message_id="$ev1", **room)
    check("a raising gate fails open", (rgb.TASKS_DIR / "task-sspace~task-G1.txt").exists())
    rgb.TASK_INTAKE_GATE = real_gate

    # 4. TTL.
    ttl_dir = tmp / "ttl"
    key = lane_dedupe.key_of({"channel_id": "!r", "source_message_id": "$ttl"})
    lane_dedupe.claim(ttl_dir, key, "a", now=1000.0, ttl=60)
    check("a live claim blocks another lane", lane_dedupe.claim(ttl_dir, key, "b", now=1059.0, ttl=60) == "a")
    check("an expired claim does not", lane_dedupe.claim(ttl_dir, key, "b", now=1061.0, ttl=60) is None)
    check("the new claim now holds the key", lane_dedupe.claim(ttl_dir, key, "a", now=1062.0, ttl=60) == "b")
    import sqlite3
    con = sqlite3.connect(str(ttl_dir / lane_dedupe.STORE_NAME))
    lane_dedupe.claim(ttl_dir, "other", "a", now=1000.0, ttl=60)
    lane_dedupe.claim(ttl_dir, "fresh", "a", now=5000.0, ttl=60)
    rows = {r[0] for r in con.execute("SELECT key FROM claims")}
    con.close()
    check("expired rows are pruned", rows == {"fresh"}, repr(rows))

    # 5. Unwritable and malformed stores fail open with one warning.
    warnings = []
    blocker = tmp / "not-a-dir"
    blocker.write_text("x")
    r1 = lane_dedupe.claim(blocker / "state", key, "a", log=warnings.append)
    r2 = lane_dedupe.claim(blocker / "state", key, "b", log=warnings.append)
    check("an unwritable store delivers", r1 is None and r2 is None)
    check("and warns once", len(warnings) == 1 and "delivering without the guard" in warnings[0], repr(warnings))
    bad = tmp / "malformed"
    bad.mkdir()
    (bad / lane_dedupe.STORE_NAME).write_bytes(b"this is not a sqlite database" * 100)
    bad_warnings = []
    check("a malformed store delivers",
          lane_dedupe.claim(bad, key, "a", log=bad_warnings.append) is None
          and lane_dedupe.claim(bad, key, "b", log=bad_warnings.append) is None)
    check("and warns once", len(bad_warnings) == 1, repr(bad_warnings))

    # 6. A real race between processes.
    race_dir = tmp / "race"
    ctx = multiprocessing.get_context("spawn")
    n = 8
    barrier, out = ctx.Barrier(n), ctx.Queue()
    procs = [ctx.Process(target=_race_worker, args=(str(race_dir), "race-key", f"lane{i}", barrier, out))
             for i in range(n)]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in range(n)]
    for p in procs:
        p.join(timeout=60)
    winners = [lane for lane, holder in results if holder is None]
    holders = {holder for _lane, holder in results if holder is not None}
    check("exactly one racing process wins", len(winners) == 1, repr(results))
    check("every loser names the winner", holders == set(winners), repr(results))

    # 7b. A seat bridge is another agent: the loader never installs the gate there.
    probe = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('rgb', {str(REPO / 'src' / 'remote-gateway-bridge.py')!r})\n"
        "m = importlib.util.module_from_spec(spec); sys.modules['rgb'] = m; spec.loader.exec_module(m)\n"
        "print('GATE', m.TASK_INTAKE_GATE is None)\n")
    env = isolated_child_env()
    env["GATEWAY_INSTANCE"] = "0" * 32
    proc = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=120)
    check("a seat bridge has no lane dedupe gate", "GATE True" in proc.stdout, proc.stdout + proc.stderr)

    print(f"\n{'FAIL' if failures else 'PASS'}: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

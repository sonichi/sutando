#!/usr/bin/env python3
"""Cross-lane duplicate guard: one room message seen by two lanes becomes one task.

Covers src/lane_dedupe.py and the gateway bridge's delegation to it:
  1. the same event on two lanes queues one task; the second lane answers its own
     task id with the redelivery control result, so the broker settles it;
  2. the same lane seeing the message again keeps today's behaviour;
  3. a task without source_message_id, or a hook task, is never deduped;
  4. claims expire after the TTL;
  5. an unwritable or malformed store fails open, with one warning line;
  6. processes racing on one key: exactly one wins (real multiprocessing), and a claim
     made while another connection holds a claim transaction open waits for it, then sees
     the key (fails with a deferred BEGIN);
  7. only same-tier copies are collapsed: lanes that disagree on the attested tier both
     deliver, with one WARNING naming both lanes and tiers;
  8. the loader installs the gate for a connection lane and never for a seat.

Run: python3 tests/gateway-lane-dedupe.test.py   (exit 0 pass / 1 fail)
"""
import importlib.util
import multiprocessing
import sqlite3
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


def _holder(*args, **kwargs):
    return lane_dedupe.claim_key(*args, **kwargs)[0]


def _race_worker(state_dir, key, lane, barrier, out):
    barrier.wait()
    out.put((lane, _holder(Path(state_dir), key, lane, log=lambda _m: None)))


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

    # 8a. Delegation: a connection lane's loader installs the module's gate.
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
    _holder(ttl_dir, key, "a", now=1000.0, ttl=60)
    check("a live claim blocks another lane", _holder(ttl_dir, key, "b", now=1059.0, ttl=60) == "a")
    check("an expired claim does not", _holder(ttl_dir, key, "b", now=1061.0, ttl=60) is None)
    check("the new claim now holds the key", _holder(ttl_dir, key, "a", now=1062.0, ttl=60) == "b")
    con = sqlite3.connect(str(ttl_dir / lane_dedupe.STORE_NAME))
    _holder(ttl_dir, "other", "a", now=1000.0, ttl=60)
    _holder(ttl_dir, "fresh", "a", now=5000.0, ttl=60)
    rows = {r[0] for r in con.execute("SELECT event FROM event_claims")}
    con.close()
    check("expired rows are pruned", rows == {"fresh"}, repr(rows))

    # 5. Unwritable and malformed stores fail open with one warning.
    warnings = []
    blocker = tmp / "not-a-dir"
    blocker.write_text("x")
    r1 = _holder(blocker / "state", key, "a", log=warnings.append)
    r2 = _holder(blocker / "state", key, "b", log=warnings.append)
    check("an unwritable store delivers", r1 is None and r2 is None)
    check("and warns once", len(warnings) == 1 and "delivering without the guard" in warnings[0], repr(warnings))
    bad = tmp / "malformed"
    bad.mkdir()
    (bad / lane_dedupe.STORE_NAME).write_bytes(b"this is not a sqlite database" * 100)
    bad_warnings = []
    check("a malformed store delivers",
          _holder(bad, key, "a", log=bad_warnings.append) is None
          and _holder(bad, key, "b", log=bad_warnings.append) is None)
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

    # 6b. Deterministic: A holds an open claim transaction after its read; B must wait, then see it.
    import threading
    lock_dir = tmp / "lock"
    _holder(lock_dir, "warmup", "x")
    event = "lock-event"
    lock_key = lane_dedupe.hashlib.sha256(f"{event}\n".encode()).hexdigest()
    a = sqlite3.connect(str(lock_dir / lane_dedupe.STORE_NAME), timeout=10, isolation_level=None)
    a.execute("BEGIN IMMEDIATE")
    check("A reads the key as free", a.execute("SELECT 1 FROM event_claims WHERE key = ?",
                                               (lock_key,)).fetchone() is None)
    b_out, b_warn = [], []
    b = threading.Thread(target=lambda: b_out.append(lane_dedupe.claim_key(
        lock_dir, event, "laneB", log=b_warn.append)))
    b.start()
    b.join(0.5)
    check("B waits while A's claim is open", b.is_alive())
    a.execute("INSERT INTO event_claims (key, event, lane, tier, ts) VALUES (?, ?, 'laneA', '', ?)",
              (lock_key, event, lane_dedupe.time.time()))
    a.execute("COMMIT")
    a.close()
    b.join(10)
    check("then B sees A's claim", b_out and b_out[0][0] == "laneA", repr((b_out, b_warn)))
    con = sqlite3.connect(str(lock_dir / lane_dedupe.STORE_NAME))
    lanes = [r[0] for r in con.execute("SELECT lane FROM event_claims WHERE event = ?", (event,))]
    con.close()
    check("and never inserts the key too", lanes == ["laneA"], repr(lanes))

    # 7. Tiers: same tier collapses, a disagreement delivers both with one WARNING.
    logs.clear()
    on_lane("", "task-T1", source_message_id="$tier1", access_tier="owner", **room)
    on_lane("sspace", "task-T2", source_message_id="$tier1", access_tier="owner", **room)
    check("the same event at the same tier on two lanes is one task",
          (rgb.TASKS_DIR / "task-T1.txt").exists() and not (rgb.TASKS_DIR / "task-sspace~task-T2.txt").exists())
    logs.clear()
    on_lane("", "task-T3", source_message_id="$tier2", access_tier="owner", **room)
    on_lane("sspace", "task-T4", source_message_id="$tier2", access_tier="team", **room)
    check("the same event at different tiers is two tasks",
          (rgb.TASKS_DIR / "task-T3.txt").exists() and (rgb.TASKS_DIR / "task-sspace~task-T4.txt").exists())
    warned = [m for m in logs if m.startswith("WARNING lane dedupe")]
    check("with one WARNING naming both lanes and tiers", len(warned) == 1
          and "as team on lane sspace" in warned[0] and "lane primary as owner" in warned[0], repr(logs))
    on_lane("", "task-T5", source_message_id="$tier3", access_tier="team", **room)
    on_lane("sspace", "task-T6", source_message_id="$tier3", access_tier="team", collaborator=True, **room)
    check("a collaborator attestation is a different tier",
          (rgb.TASKS_DIR / "task-sspace~task-T6.txt").exists())

    # 8. A seat bridge is another agent: the loader never installs the gate there.
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

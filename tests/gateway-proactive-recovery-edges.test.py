#!/usr/bin/env python3
"""Recovery edges of the proactive outbox lane, each a way a message could post twice or be lost:

  1. two bodies parked under one file name -> the logged command for the first restores and
     sends only the first, once
  2. a restart past the retry deadline     -> a recorded receipt settles DELIVERED, an
     interrupted send parks outcome-unknown; neither is spent as an exhausted retry
  3. a crash right after the final refusal  -> the operator's requeue sends it (no stale mark)
  4. the owner DM changes after a refusal   -> the item is held, never re-routed, and goes to
     its own room once they agree again

Run: python3 tests/gateway-proactive-recovery-edges.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
import json
import shlex
import subprocess
import urllib.error

from _helpers import proactive_lane as pl
from _helpers.hermetic_env import isolated_child_env
from ag2_sparrow import outbox as vendored

REPO = Path(__file__).resolve().parents[1]
ISO = pl.scratch("gw-proactive-recovery-")
check = pl.Checks()


class Crash(BaseException):
    pass


def timeout(payload):
    raise urllib.error.URLError(TimeoutError("timed out"))


def refused(payload):
    raise urllib.error.HTTPError("https://gw.example/relay/v1/room", 429, "slow down", {}, None)


def run_logged(lane, stem: str, nth: int = 0):
    lines = [m for m in lane.logs if "outbox_cli.py" in m and stem in m]
    cmd = shlex.split(lines[nth].rsplit(": ", 1)[-1]) if len(lines) > nth else []
    if not cmd:
        return None
    return subprocess.run([sys.executable if c == "python3" else c for c in cmd], cwd=REPO,
                          env=isolated_child_env(), capture_output=True, text=True, timeout=60)


def die_as_restart(lane, item_id: str) -> None:
    """The claim's owner is a dead process past its reclaim window, and the file is pending."""
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    claim_file = vendored._claim_path(lane.results / ".outbox-ag2space-proactive", item_id)
    if claim_file.exists():
        data = json.loads(claim_file.read_text())
        claim_file.write_text(json.dumps({**data, "pid": dead.pid, "start_usec": None,
                                          "claimed_at": data["claimed_at"] - 3600}))
    for c in lane.results.glob("proactive-*.sending.*"):
        c.rename(lane.results / (c.name.split(".sending.")[0] + ".txt"))
    lane.restart()


def two_generations() -> None:
    lane = pl.Lane(gb, ISO, timeout)
    lane.write("proactive-1.txt", "body A\n")
    lane.drain()
    lane.write("proactive-1.txt", "body B\n")
    lane.drain()
    run = run_logged(lane, "proactive-1", 0)
    check(run is not None and run.returncode == 0, f"1. A's logged command runs ({run and run.stderr[-200:]})")
    restored = lane.results / "proactive-1.txt"
    check(restored.exists() and restored.read_text() == "body A\n",
          f"1. it restores A's body, not the newest ({restored.read_text() if restored.exists() else None!r})")
    lane.respond = lambda payload: {"ok": True, "event_id": "$A"}
    n = len(lane.posts)
    for _ in range(3):
        lane.drain()
    check([p.get("body") for p in lane.posts[n:]] == ["body A"],
          f"1. only A is sent, once ({[p.get('body') for p in lane.posts[n:]]})")
    left = sorted(p.read_text() for p in (lane.results / "undelivered").glob("proactive-1-*.txt"))
    check(left == ["body B\n"], f"1. B stays parked for its own recovery ({left})")


def past_the_deadline(with_receipt: bool) -> None:
    label = "with a receipt" if with_receipt else "interrupted, no receipt"
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-2.txt", "late\n")
    backend = lane.backend()
    now = [1_000_000.0]
    backend.clock = lambda: now[0]
    for t in (0, 2, 6, 14, 30):
        now[0] = 1_000_000.0 + t
        lane.drain()
    now[0] = 1_000_000.0 + 60
    item_id = gb._proactive_item_id("proactive-2", "late")
    real = gb._record_proactive_receipt
    if with_receipt:
        lane.respond = lambda payload: {"ok": True, "event_id": "$landed"}

        def record_then_die(*a):
            real(*a)
            raise Crash()
        gb._record_proactive_receipt = record_then_die
    else:
        def die(payload):
            raise Crash()
        lane.respond = die
    try:
        lane.drain()
    except Crash:
        pass
    finally:
        gb._record_proactive_receipt = real
    posts = len(lane.posts)
    die_as_restart(lane, item_id)
    backend = lane.backend()
    backend.clock = lambda: 1_000_000.0 + 700
    lane.respond = lambda payload: {"ok": True, "event_id": "$again"}
    for _ in range(3):
        lane.drain()
    item = vendored.read_item(lane.results / ".outbox-ag2space-proactive", item_id) or {}
    want = ("DELIVERED", None) if with_receipt else ("PARKED", "outcome-unknown")
    check((item.get("status"), item.get("reason")) == want and len(lane.posts) == posts,
          f"2. {label}: {want} after a restart past the deadline, no new POST "
          f"({item.get('status')}, {item.get('reason')}, {len(lane.posts) - posts} new)")


def crash_after_final_refusal() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-3.txt", "three\n")
    backend = lane.backend()
    now = [2_000_000.0]
    backend.clock = lambda: now[0]
    for t in (0, 2, 6, 14):
        now[0] = 2_000_000.0 + t
        lane.drain()
    now[0] = 2_000_000.0 + 601
    real = vendored.clear_dispatching

    def die(*a, **k):
        raise Crash()
    vendored.clear_dispatching = die
    try:
        lane.drain()
    except Crash:
        pass
    finally:
        vendored.clear_dispatching = real
    item_id = gb._proactive_item_id("proactive-3", "three")
    root = lane.results / ".outbox-ag2space-proactive"
    item = vendored.read_item(root, item_id) or {}
    check(item.get("status") == "PARKED" and "dispatching" not in item,
          f"3. the final refusal parks in one write, the mark gone with it ({item.get('reason')}, {'dispatching' in item})")
    vendored.requeue_item_with_epoch(root, item_id, reset_attempts=True, operator="test", reason="operator")
    for c in lane.results.glob("proactive-3.sending.*"):
        c.rename(lane.results / "proactive-3.txt")
    lane.respond = lambda payload: {"ok": True, "event_id": "$ok"}
    n = len(lane.posts)
    lane.drain()
    item = vendored.read_item(root, item_id) or {}
    check(len(lane.posts) == n + 1 and item.get("status") == "DELIVERED",
          f"3. the operator's requeue sends it ({len(lane.posts) - n} POST, {item.get('status')}, {item.get('reason')})")


def dm_changes_after_refusal() -> None:
    lane = pl.Lane(gb, ISO, refused)
    lane.write("proactive-4.txt", "four\n")
    lane.drain()
    lane.owner_dm = "!NewOwnerDm:ag2.space"
    lane.respond = lambda payload: {"ok": True, "event_id": "$ok"}
    backend = lane.backend()
    backend.clock = lambda: gb.time.time() + 120
    n = len(lane.posts)
    lane.drain()
    lane.drain()
    check(len(lane.posts) == n and lane.where("proactive-4") == "pending",
          f"4. held after the DM moved, not re-routed ({len(lane.posts) - n} POSTs, {lane.where('proactive-4')})")
    check(any("HELD" in m and pl.OWNER_DM in m and "!NewOwnerDm:ag2.space" in m for m in lane.logs),
          "4. the hold names the stored and the new room")
    lane.owner_dm = pl.OWNER_DM
    lane.drain()
    check(len(lane.posts) == n + 1 and lane.posts[-1].get("room_id") == pl.OWNER_DM,
          f"4. once they agree it goes to its own room ({lane.posts[-1].get('room_id') if lane.posts else None})")


def main() -> int:
    two_generations()
    past_the_deadline(with_receipt=True)
    past_the_deadline(with_receipt=False)
    crash_after_final_refusal()
    dm_changes_after_refusal()
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())

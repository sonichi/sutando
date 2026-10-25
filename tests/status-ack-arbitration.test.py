#!/usr/bin/env python3
"""The core's notify and the status-ack runner share ack:<task>:initial: exactly one of them speaks.

Both sides run their production code: notify.py's main() (its room POST captured at _post) and
the runner's run_once(), whose line goes through the gateway's real proactive outbox drain.

  a) runner first: one POST in the thread; the core's first notify is suppressed (rc 0, no POST)
  b) after (a): the core's second notify is progress and goes out; the real answer is untouched
  c) notify first: one POST from notify; the runner, past the deadline, publishes nothing
  d) neither the gateway bridge nor the result path reads the ack claim
  e) a claimed/assigned rename of the task keys the claim on the canonical id, either sender first
  f) a pool worker's notify from its deliveries/<seat>/ sentinel arbitrates on the canonical key
  g) a --task-file outside tasks/ (or not a sentinel under deliveries/) is never arbitrated
  CONTROL: a runner that ignores the shared claim makes (c) two POSTs; so does a notify
           that takes no claim in (a), so the one-POST check bites

Run: python3 tests/status-ack-arbitration.test.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gb, _IMPORT_READS = isolate_then_import(env={"REMOTE_PROACTIVE_ROOM": ""})
from _helpers import proactive_lane as pl  # noqa: E402
from _helpers import status_ack_env as sa  # noqa: E402

import ack_claim  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ISO = pl.scratch("status-ack-arbitration-")
check = pl.Checks()
mod = sa.runner()


def load_notify():
    spec = importlib.util.spec_from_file_location("notify_under_test",
                                                  REPO / "skills" / "task-progress" / "scripts" / "notify.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


notify = load_notify()
NOTIFY_POSTS: list = []
notify._post = lambda url, payload, headers, timeout=10: NOTIFY_POSTS.append(dict(payload)) or True


def run_notify(task_file: Path, message: str) -> int:
    saved = sys.argv
    sys.argv = ["notify.py", "--task-file", str(task_file), "--message", message]
    try:
        return notify.main()
    finally:
        sys.argv = saved


def runner_pass(lane, now, ignore_claim=False):
    """`ignore_claim`: the CONTROL runner that neither reads nor takes the shared claim."""
    saved = mod.ack_claim.claim, mod.ack_claim.state
    if ignore_claim:
        mod.ack_claim.claim, mod.ack_claim.state = (lambda *a, **k: True), (lambda *a, **k: "none")
    try:
        return mod.run_once(lane.ws, sa.on(mod), now=now, holders=sa.SESSION)
    finally:
        mod.ack_claim.claim, mod.ack_claim.state = saved


def main() -> int:
    tid = "task-1761300000001"
    lane = pl.Lane(gb, ISO)
    tf = sa.task(lane.ws, tid, thread_root=sa.ROOT_EVT)
    runner_pass(lane, sa.T0 + 130)
    lane.drain()
    check(len(lane.posts) == 1 and lane.posts[0].get("thread_root") == sa.ROOT_EVT,
          f"a) the runner's line is one POST in the task's thread ({lane.posts})")
    del NOTIFY_POSTS[:]
    rc = run_notify(tf, "On it, looking into that now.")
    check(rc == 0 and NOTIFY_POSTS == [], f"a) the core's first notify is suppressed (rc {rc}, {NOTIFY_POSTS})")
    rc = run_notify(tf, "Done with the research, writing up now.")
    check(rc == 0 and len(NOTIFY_POSTS) == 1 and NOTIFY_POSTS[0].get("body", "").startswith("Done with"),
          f"b) a later notify is progress and still goes out ({NOTIFY_POSTS})")
    result = lane.results / f"{tid}.txt"
    result.write_text("Here is the answer.\n", encoding="utf-8")
    runner_pass(lane, sa.T0 + 400)
    lane.drain()
    check(result.read_text(encoding="utf-8") == "Here is the answer.\n" and len(lane.posts) == 1,
          "b) the real answer stays for its own path, and no second status line follows it")

    def one_post_when_notify_first(ignore_claim=False) -> int:
        del NOTIFY_POSTS[:]
        lane2 = pl.Lane(gb, ISO)
        tf2 = sa.task(lane2.ws, "task-1761300000002")
        run_notify(tf2, "On it.")
        runner_pass(lane2, sa.T0 + 130, ignore_claim)
        lane2.drain()
        return len(NOTIFY_POSTS) + len(lane2.posts)

    total = one_post_when_notify_first()
    check(total == 1, f"c) notify first: one POST in all ({total})")
    lane3 = pl.Lane(gb, ISO)
    tf3 = sa.task(lane3.ws, "task-1761300000003")
    run_notify(tf3, "On it.")
    held = ack_claim.read(lane3.ws, "ack:task-1761300000003:initial") or {}
    check(held.get("holder") == "task-progress" and held.get("state") == "confirmed",
          f"c) notify holds the initial ack, confirmed by its send ({held.get('holder')}, {held.get('state')})")
    r = runner_pass(lane3, sa.T0 + 130)
    check(not r.spoken and sa.proactive_files(lane3.ws) == [],
          f"c) the runner publishes nothing ({r.skipped.get('task-1761300000003')!r})")

    bridge = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
    check("ack_claim" not in bridge and "ack-claims" not in bridge,
          "d) the gateway (results and proactive drains) never reads the ack claim")

    for suffix in (".claimed-core-2", ".assigned-worker1"):
        for order in ("runner first", "notify first"):
            rid = "task-1761300000010"
            lane5 = pl.Lane(gb, ISO)
            p5 = sa.task(lane5.ws, rid, thread_root=sa.ROOT_EVT)
            tf5 = p5.rename(p5.with_name(f"{rid}{suffix}.txt"))
            del NOTIFY_POSTS[:]
            if order == "runner first":
                runner_pass(lane5, sa.T0 + 130)
                lane5.drain()
                run_notify(tf5, "On it.")
            else:
                run_notify(tf5, "On it.")
                runner_pass(lane5, sa.T0 + 130)
                lane5.drain()
            total = len(lane5.posts) + len(NOTIFY_POSTS)
            check(total == 1 and ack_claim.state(lane5.ws, f"ack:{rid}:initial") != "none",
                  f"e) {suffix} {order}: one POST in all, on the canonical key ({total})")
            check(ack_claim.state(lane5.ws, f"ack:{rid}{suffix}:initial") == "none",
                  f"e) {suffix} {order}: no key is minted from the renamed file's stem")
    for order in ("notify first", "runner first"):
        rid = "task-1761300000020"
        lane7 = pl.Lane(gb, ISO)
        sa.task(lane7.ws, rid, thread_root=sa.ROOT_EVT)
        seat = lane7.ws / "deliveries" / "worker1"
        seat.mkdir(parents=True, exist_ok=True)
        sentinel = seat / f"{rid}.accepted"
        sentinel.touch()
        del NOTIFY_POSTS[:]
        if order == "runner first":
            runner_pass(lane7, sa.T0 + 130)
            lane7.drain()
            run_notify(sentinel, "On it.")
        else:
            rc7 = run_notify(sentinel, "On it.")
            held7 = ack_claim.read(lane7.ws, f"ack:{rid}:initial") or {}
            check(rc7 == 0 and len(NOTIFY_POSTS) == 1 and NOTIFY_POSTS[0].get("thread_root") == sa.ROOT_EVT
                  and held7.get("holder") == "task-progress",
                  f"f) the worker's first notify posts in the thread and holds the claim (rc {rc7}, {held7})")
            r7 = runner_pass(lane7, sa.T0 + 130)
            lane7.drain()
            check(not r7.spoken and sa.proactive_files(lane7.ws) == [],
                  f"f) the runner stays silent for the worker-held task ({r7.skipped.get(rid)!r})")
        total = len(lane7.posts) + len(NOTIFY_POSTS)
        check(total == 1, f"f) deliveries sentinel, {order}: one POST in all ({total})")

    lane8 = pl.Lane(gb, ISO)
    rid = "task-1761300000021"
    body8 = sa.task(lane8.ws, rid).read_text(encoding="utf-8")
    seat8 = lane8.ws / "deliveries" / "worker1"
    seat8.mkdir(parents=True, exist_ok=True)
    for other in (lane8.ws / "notes" / f"{rid}.txt", seat8 / "notes.txt"):
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text(body8, encoding="utf-8")
        del NOTIFY_POSTS[:]
        rc8 = run_notify(other, "On it.")
        check(rc8 == 0 and len(NOTIFY_POSTS) == 1 and ack_claim.state(lane8.ws, f"ack:{rid}:initial") == "none",
              f"g) {other.parent.name}/{other.name}: sent, no claim taken (rc {rc8}, {len(NOTIFY_POSTS)} POSTs)")

    saved_arb, notify._ACK_ARBITRATION = notify._ACK_ARBITRATION, (
        notify._ACK_ARBITRATION[0], notify._ACK_ARBITRATION[1], lambda path: Path(path).stem)
    try:
        rid = "task-1761300000011"
        lane6 = pl.Lane(gb, ISO)
        p6 = sa.task(lane6.ws, rid)
        tf6 = p6.rename(p6.with_name(f"{rid}.claimed-core-2.txt"))
        del NOTIFY_POSTS[:]
        runner_pass(lane6, sa.T0 + 130)
        lane6.drain()
        run_notify(tf6, "On it.")
        both6 = len(lane6.posts) + len(NOTIFY_POSTS)
    finally:
        notify._ACK_ARBITRATION = saved_arb
    check(both6 == 2, f"CONTROL: a notify keyed by the file stem speaks after the runner ({both6} POSTs)")

    saved, notify._ACK_ARBITRATION = notify._ACK_ARBITRATION, None
    try:
        del NOTIFY_POSTS[:]
        lane4 = pl.Lane(gb, ISO)
        tf4 = sa.task(lane4.ws, "task-1761300000004")
        runner_pass(lane4, sa.T0 + 130)
        lane4.drain()
        run_notify(tf4, "On it.")
        both = len(lane4.posts) + len(NOTIFY_POSTS)
    finally:
        notify._ACK_ARBITRATION = saved
    check(both == 2, f"CONTROL: a notify that takes no claim speaks after the runner ({both} POSTs)")
    broken = one_post_when_notify_first(ignore_claim=True)
    check(broken == 2, f"CONTROL: a runner ignoring the shared claim speaks too ({broken} POSTs)")
    return check.exit_code()


if __name__ == "__main__":
    sys.exit(main())

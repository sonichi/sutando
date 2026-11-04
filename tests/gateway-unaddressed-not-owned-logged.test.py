#!/usr/bin/env python3
"""A lane that leaves an unaddressed proactive message for the owning lane says so in its log.

Reported (#5981): two results/proactive-*.txt files sat undelivered for over two hours on a host
whose only running bridge was a secondary lane, and its log had no line for either file. A file
naming no room belongs to the lane that owns unaddressed messages; the others skipped it silently.
  a) a secondary lane does not send the file and leaves it in results/
  b) it logs the skip, naming the file and the lane, on the 1st, 2nd and 4th pass (backed off)
  c) control: the owning lane delivers the same file to the owner DM, with no such line

Run: python3 tests/gateway-unaddressed-not-owned-logged.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
sys.path.insert(0, str(REPO / "src"))
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="gw-unaddr-cc-")
os.environ["REMOTE_PROACTIVE_ROOM"] = ""

from ag2_sparrow import remote_gateway_bridge as gb  # noqa: E402

OWNER_DM = "!OwnerDmRoomXyZ:s.space"
SKIP = "left for the lane that owns unaddressed messages"
FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def results_with_unaddressed() -> Path:
    results = Path(tempfile.mkdtemp(prefix="gw-unaddr-results-"))
    (results / "archive").mkdir()
    (results / "proactive-orphan-check-1.txt").write_text("Two tasks were left unanswered at startup.\n")
    return results


def drain(results: Path, instance: str, owns, posts: list, logs: list) -> None:
    def fake_req(method, path, payload=None, timeout=None, **_kw):
        if method == "POST":
            posts.append(payload)
        return {"ok": True, "event_id": "$evt"}

    saved = (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.PROACTIVE_ROOM,
             gb._req, gb.PROACTIVE_CLAIM_GATE, gb._STATE, gb.GATEWAY_INSTANCE, gb._log, gb.OWNS_UNADDRESSED)
    gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR = results, results / "archive"
    gb.UNDELIVERABLE_RESULTS_DIR = results / "undelivered"
    gb.PROACTIVE_ROOM, gb._req, gb.PROACTIVE_CLAIM_GATE = OWNER_DM, fake_req, None
    gb._STATE = Path(tempfile.mkdtemp(prefix="gw-unaddr-state-"))
    gb.GATEWAY_INSTANCE, gb._log, gb.OWNS_UNADDRESSED = instance, logs.append, owns
    gb._ROUTING.update(owner_dm=OWNER_DM, loaded=True, next=gb.time.time() + 3600)
    try:
        gb._post_proactive()
    finally:
        (gb.RESULTS_DIR, gb.ARCHIVE_RESULTS_DIR, gb.UNDELIVERABLE_RESULTS_DIR, gb.PROACTIVE_ROOM,
         gb._req, gb.PROACTIVE_CLAIM_GATE, gb._STATE, gb.GATEWAY_INSTANCE, gb._log,
         gb.OWNS_UNADDRESSED) = saved


def main() -> int:
    gb._LOG_COUNTS.clear()
    results, posts, logs = results_with_unaddressed(), [], []
    for _ in range(4):
        drain(results, "s_dspace", lambda: False, posts, logs)
    left = [m for m in logs if SKIP in m]
    check(posts == [] and (results / "proactive-orphan-check-1.txt").exists(),
          "a) the secondary lane does not send it, and the file stays in results/")
    check(len(left) == 3, f"b) the skip is logged on the 1st, 2nd and 4th pass ({len(left)} lines)")
    check(bool(left) and "proactive-orphan-check-1.txt" in left[0] and "(s_dspace)" in left[0],
          f"b) the line names the file and the lane ({left[:1]})")
    check(bool(left) and "seen 4 times" in left[-1], f"b) the last line carries the count ({left[-1:]})")

    gb._LOG_COUNTS.clear()
    results, posts, logs = results_with_unaddressed(), [], []
    drain(results, "", lambda: True, posts, logs)
    check(len(posts) == 1 and posts[0].get("room_id") == OWNER_DM,
          f"c) the owning lane delivers it to the owner DM ({posts})")
    check(not any(SKIP in m for m in logs), "c) and logs no skip line")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print("all unaddressed-skip logging checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

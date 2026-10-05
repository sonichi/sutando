#!/usr/bin/env python3
"""A recipient folder marked non-exclusive holds claims, not deliveries.

A side service can claim a task in `deliveries/<name>/` so the core's held-readers
(worker_holds, holder_of, the Stop hook) leave the task alone while it works. If that
task is also delivered to a bound worker, the router used to see two holders and refuse
it ("refusing to choose"), so the watcher published a terminal failure for it. With
`pool_delivery.NON_EXCLUSIVE_MARKER` in the folder:

- the router reads only the worker as the task's holder, delivers nothing new, and a
  pass over that task and another room's task routes both;
- the handler's run returns 0 for it, not MUST_HANDLE;
- the held-readers still count the claim as held;
- an UNMARKED folder still conflicts, exactly as before.
"""
from __future__ import annotations

import atexit
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_delivery as pd  # noqa: E402
import pool_route_handler as h  # noqa: E402

import pool_router as rt  # noqa: E402
import worker_delivery as wd  # noqa: E402

from delivery import task_dispatch as td  # noqa: E402

W = "d" * 32
OTHER = "e" * 32
DESK_ROOM = "!desk:example.test"
OTHER_ROOM = "!other:example.test"
CLAIMS = "side-claims"
FAILURES: list[str] = []
_TREES: list[Path] = []
atexit.register(lambda: [shutil.rmtree(t, ignore_errors=True) for t in _TREES])


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def workspace(marked: bool) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="router-non-exclusive-"))
    _TREES.append(tmp)
    ws = tmp / "ws"
    for d in ("tasks", "state", "deliveries"):
        (ws / d).mkdir(parents=True)
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"version": 1, "workers": {W: {"state": "live"}, OTHER: {"state": "live"}},
         "bindings": {DESK_ROOM: W, OTHER_ROOM: OTHER}}))
    for tid, room in (("task-desk", DESK_ROOM), ("task-other", OTHER_ROOM)):
        (ws / "tasks" / f"{tid}.txt").write_text(
            f"id: {tid}\nchannel_id: {room}\nsource: ag2space\naccess_tier: owner\ntask: body\n")
    # The desk task reached its worker first; the side service then claimed it.
    rt.route(ws, {"id": "task-desk", "channel_id": DESK_ROOM, "source": "ag2space"})
    claims = ws / "deliveries" / CLAIMS
    claims.mkdir()
    if marked:
        (claims / pd.NON_EXCLUSIVE_MARKER).write_text("")
    (claims / "task-desk.accepted").write_text("")
    return ws


def tasks() -> list:
    return [{"id": "task-desk", "channel_id": DESK_ROOM, "source": "ag2space"},
            {"id": "task-other", "channel_id": OTHER_ROOM, "source": "ag2space"}]


def scenario_marked_folder_is_not_a_holder() -> None:
    print("\nscenario: worker sentinel + a claim in a MARKED folder")
    ws = workspace(marked=True)
    try:
        out = rt.route_all(ws, tasks())
    except rt.RouterRefused as e:
        check("route_all over the desk task and another room's task does not raise", False, repr(e))
        return
    by_id = {o["task_id"]: o for o in out}
    check("route_all over the desk task and another room's task does not raise", True)
    check("the desk task stays with its worker, nothing new delivered",
          by_id["task-desk"]["already"] == [W] and by_id["task-desk"]["delivered"] == [], str(by_id["task-desk"]))
    check("the other room's task in the same pass is delivered",
          by_id["task-other"]["delivered"] == [OTHER], str(by_id["task-other"]))
    check("no pending sentinel was created in the claims folder",
          not (ws / "deliveries" / CLAIMS / "task-desk.txt").exists())
    rc = h.main(["--workspace", str(ws), "--task-file", str(ws / "tasks" / "task-desk.txt")])
    check("the handler's run for the desk task returns 0, not MUST_HANDLE", rc == 0, f"rc={rc}")
    check("worker_holds still counts the task as held", td.worker_holds(ws / "deliveries", "task-desk.txt"))
    (ws / "deliveries" / W / "task-desk.txt").unlink()
    check("with only the claim left, holder_of names the claims folder",
          wd.holder_of(ws, "task-desk") == CLAIMS, str(wd.holder_of(ws, "task-desk")))
    check("...and worker_holds still counts it", td.worker_holds(ws / "deliveries", "task-desk.txt"))


def scenario_unmarked_folder_still_conflicts() -> None:
    print("\nscenario: control — the same claim in an UNMARKED folder")
    ws = workspace(marked=False)
    try:
        rt.route_all(ws, tasks())
        check("route_all refuses the two-holder task", False, "no refusal")
    except rt.ConflictingDelivery as e:
        check("route_all refuses the two-holder task", "refusing to choose" in str(e), str(e))
    rc = h.main(["--workspace", str(ws), "--task-file", str(ws / "tasks" / "task-desk.txt")])
    check("the handler's run returns MUST_HANDLE", rc == h.MUST_HANDLE, f"rc={rc}")


def scenario_unreadable_marker_refuses() -> None:
    print("\nscenario: a marker that is not a regular file is not read as either answer")
    ws = workspace(marked=False)
    (ws / "deliveries" / CLAIMS / pd.NON_EXCLUSIVE_MARKER).mkdir()
    try:
        rt.holders(ws, "task-desk")
        check("holders refuses", False, "no refusal")
    except rt.UnreadableEvidence as e:
        check("holders refuses", "refusing to decide" in str(e), str(e))


if __name__ == "__main__":
    scenario_marked_folder_is_not_a_holder()
    scenario_unmarked_folder_still_conflicts()
    scenario_unreadable_marker_refuses()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'all passed'}")
    sys.exit(1 if FAILURES else 0)

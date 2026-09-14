#!/usr/bin/env python3
"""The router, as the core watcher's task-event handler.

One process, two watches: the core's watcher already watches `tasks/`, so the
router runs inside it rather than as a second daemon. This file is dispatch
only — every routing decision belongs to `pool_router`/`pool_roster`.

The watcher's handler protocol carries the design's recipient rules exactly:

    probe 3   decline    not a roster hit -- unbound, or a name never created.
                          The core takes it; it is a recipient, not a fallback.
    probe 0   accept     every target is on the roster; the real run delivers.
    4         must-handle a header id that is not the file's, or an admitted
                          target left without a sentinel: the core must not
                          inherit it, and 0 would release the claim on nothing.

Run: called by src/watch-tasks-stream.sh; see dispatch_task there.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# local_task_protocol lives in the core; repo root is parents[3] from here
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import local_task_protocol as ltp  # noqa: E402

import pool_roster as pr  # noqa: E402
import worker_picker_commands as wpc  # noqa: E402

import pool_router as rt  # noqa: E402
import pool_advertise as pa

DECLINE = 3
MUST_HANDLE = 4
PICKER_WIRE = "worker-picker"


def read_task(task_file: str) -> dict:
    """The watcher hands a task FILE; the router takes a task DICT.

    `requested_worker` is read only from ABOVE `task:`, so a body cannot forge
    it. `channel_id`/`source` are read leniently: the gateway stamps them
    below `task:`, where the strict parse never looks.
    """
    text = Path(task_file).read_text(encoding="utf-8", errors="replace")
    task: dict = {"id": Path(task_file).stem}
    for line in text.splitlines():
        if line.startswith("task:"):
            break
        key, _, value = line.partition(":")
        if _ and key.strip() in ("id", "channel_id", "source", "requested_worker"):
            task[key.strip()] = value.strip()
    if not task.get("channel_id") or not task.get("source") or "wire_source" not in task:
        lenient = ltp.parse_task_headers_lenient(text).headers
        for k in ("channel_id", "source", "wire_source"):
            if not task.get(k) and lenient.get(k):
                task[k] = str(lenient.get(k)).strip()
    return task


def classify(workspace, task: dict) -> tuple[int, list, dict | None]:
    """(exit code, targets, the roster they were read from) without delivering.

    The roster is returned so the run routes against the SAME snapshot that
    admitted the task: reloaded, a binding removed in between sends the task
    to the core under an exit code that says a worker has it.
    """
    # A picker command edits the bindings; routing it BY a binding would hand
    # a bound room's unpin to the worker it unpins. The core is the controller.

    # Two writer generations: the lane-authority writer stamps wire_source,
    # the older one stamps source itself. Either mark means the same command.
    if PICKER_WIRE in (task.get("wire_source"), task.get("source")):
        return DECLINE, [], None
    try:
        raw = pr._load_existing_roster_strict(workspace)
    except pr.RosterError:
        # Absent means no pool; UNREADABLE means we cannot tell whose work this
        # is. Declining would hand every bound task to the unrestricted core.
        return MUST_HANDLE, [], None
    roster = raw if (isinstance(raw, dict) and "workers" in raw) else None
    if roster is None:
        return DECLINE, [], None
    targets = pr.targets_for(roster, task.get("channel_id") or task.get("source") or "",
                             task.get("requested_worker"))
    # One question only: is every target on the roster? Anything else -- no
    # binding, a name never created -- is the core's, which is a real recipient.
    if targets == [pr.CORE] or pr.unknown_targets(roster, targets):
        return DECLINE, targets, roster
    # Liveness is deliberately NOT asked: the sentinel is durable, so a worker
    # that starts later finds its work.
    return 0, targets, roster


def apply_picker(workspace, task_file, results_dir=None) -> "dict | None":
    """A pin is live the moment it arrives: applied and published here, at the
    edge, so the bridge ships the new binding without waiting for another
    task. Runs on the probe as well: the watcher probes once and, on DECLINE,
    hands the task straight to the core, so the probe is the only call a
    picker task gets -- EXCEPT across a restart, where the startup sweep
    re-probes every retained task, so the replay gate is what makes that safe.
    Idempotent; a failure is reported, never fatal."""
    try:
        cmd = wpc.authorized_command(task_file)
        out = wpc.apply(workspace, cmd, task_id=Path(task_file).stem,
                        results_dir=results_dir) if cmd else None
    except (pr.RosterError, OSError, ValueError) as e:
        print(f"pool_route_handler: picker command not applied: {e}", file=sys.stderr)
        return None
    if out and out.get("action") == "skipped":
        print(f"pool_route_handler: picker command for {out['room']} not replayed: "
              f"{out['reason']}", file=sys.stderr)
    elif out:
        print(f"pool_route_handler: applied {out['action']} for {out['room']} "
              f"(roster v{out['roster_version']}, advertisement written)", file=sys.stderr)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--task-file", required=True)
    p.add_argument("--workspace", default=None)
    p.add_argument("--probe", action="store_true")
    # The watcher passes its RESOLVED results dir; the replay gate reads it, so
    # it is a real argument here rather than one parsed and thrown away.
    p.add_argument("--results-dir", default=None)
    for ignored in ("--runtime", "--repo"):
        p.add_argument(ignored, default=None)
    args, _unknown = p.parse_known_args(argv)

    ws = args.workspace
    # An inherited roster has no advertisement until something publishes it;
    # the edge is here, and a failure to publish must never stop routing.
    try:
        pa.ensure_advertisement(ws)
    except Exception as e:  # noqa: BLE001 — ANY failure, per the contract above:
        # an escape here routes a BOUND task to the unrestricted live core.
        print(f"pool_route_handler: advertisement not ensured: {e!r}", file=sys.stderr)
    task = read_task(args.task_file)
    if PICKER_WIRE in (task.get("wire_source"), task.get("source")):
        apply_picker(ws, args.task_file, args.results_dir)
    code, targets, roster = classify(ws, task)
    stem = Path(args.task_file).stem
    if code == 0 and task["id"] != stem:
        # The router delivers BY id: a header naming another file delivers
        # nothing, and the shared writer rejects the sentinel anyway.
        print(f"pool_route_handler: header id {task['id']!r} is not the file's "
              f"{stem!r}; refusing", file=sys.stderr)
        return MUST_HANDLE
    if args.probe:
        return code
    if code == DECLINE:
        return DECLINE

    try:
        out = rt.route(ws, task, roster)
    except rt.RouterRefused as e:
        # The pass refused; the core must not silently inherit the task.
        print(f"pool_route_handler: {e}", file=sys.stderr)
        return MUST_HANDLE
    except OSError as e:
        # A delivery I/O failure is still "a worker holds this": rc 1 would read
        # as an optional decline and hand the task to the unrestricted core.
        print(f"pool_route_handler: delivery failed: {e}", file=sys.stderr)
        return MUST_HANDLE
    settled = set(out.get("delivered") or []) | set(out.get("already") or [])
    unsettled = [t for t in targets if t not in settled] + list(out.get("skipped") or [])
    if unsettled:
        # 0 here releases the watcher's claim on a task no worker holds.
        print(json.dumps({**out, "unsettled": unsettled}), file=sys.stderr)
        return MUST_HANDLE
    return 0


def guarded_main(argv=None) -> int:
    """`main` for the watcher, failing CLOSED on anything it did not anticipate.

    A handler that crashed did not settle the task, and any other non-zero code
    reads as an optional decline — which hands a bound task to the live core.
    """
    try:
        return main(argv)
    except SystemExit as e:
        # argparse exits rather than raising, and SystemExit is a BaseException:
        # an `Exception` floor moves this fail-open instead of closing it.
        return MUST_HANDLE if (e.code or 0) != 0 else 0
    except Exception as e:  # noqa: BLE001 — the exit code IS the routing decision
        print(f"pool_route_handler: unhandled {e!r}", file=sys.stderr)
        return MUST_HANDLE


if __name__ == "__main__":
    raise SystemExit(guarded_main())

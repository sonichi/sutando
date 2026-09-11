#!/usr/bin/env python3
"""The router, as the core watcher's task-event handler.

One process, two watches: the core's watcher already watches `tasks/`, so the
router runs inside it rather than as a second daemon. This file is dispatch
only — every routing decision belongs to `pool_router`/`pool_roster`.

The watcher's handler protocol carries the design's recipient rules exactly:

    probe 3   decline      not a roster hit -- unbound, or a name never created.
                            The core takes it; it is a recipient, not a fallback.
    probe 0   taken        every target is on the roster, OR the roster cannot be
                            read. The real run delivers; if it fails the task stays
                            the worker's under a retry marker and the run still
                            exits 0 -- the core repairs delivery, it never answers
                            a worker's task (owner rule). Every real run re-delivers
                            marked tasks (oldest first, bounded) BEFORE its own, so
                            the next event is the retry driver.
    run   5   unsettled    no marker store took the marker, so no pass can find the
                            task. The watcher KEEPS its claim: exit 0 would settle
                            unrecoverable work and any other non-zero would fall
                            back to the core.

A declined event queues no real run, so the watcher drives the same pass over
`--retry-pass` on that branch too: a host whose traffic is all unbound would
otherwise never re-deliver deferred work. The probe itself stays read-only.

Run: called by src/watch-tasks-stream.sh; see dispatch_task there.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_task_protocol as ltp  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

import pool_roster as pr  # noqa: E402

import pool_router as rt  # noqa: E402

DECLINE = 3
TAKE = 0
# A run that could record the task's ownership in NO durable store. The watcher
# keeps its claim on this code: settling would strand it, falling back is banned.
UNSETTLED = 5
# `--parked` could not answer. Kept out of the 0/1 pair for the same reason
# pool_delivery keeps HELD_UNKNOWN out of it: unknown is not a negative.
PARKED_UNKNOWN = 2

# Every one of these selects a recipient, so every one is header-only.
ROUTING_KEYS = ("id", "channel_id", "source", "access_tier", "requested_worker")

# A run that inherited a backlog must still handle the task that woke it; the
# rest waits for the next event rather than stalling this one.
RETRY_LIMIT = 20

_UNSET = object()

# Said on stderr, which the watcher's own log keeps: a deferral with no marker
# is a deferral no retry pass can find, and silence there reads as recovered.
UNMARKED_NOTICE = "pool-route-handler: DEFERRED WITHOUT RETRY MARKER"

# Verbs that answer a question without routing anything, so `run` leaves no
# per-call line: the Stop hook asks `--parked` once per task on every stop.
READ_ONLY_FLAGS = ("--probe", "--parked")


def read_task(task_file: str) -> dict:
    """The watcher hands a task FILE; the router takes a task DICT.

    Every field here selects a recipient, so every field comes from the STRICT
    parse: `task:` is the last header and nothing below it is a header.
    """
    text = Path(task_file).read_text(encoding="utf-8", errors="replace")
    headers = ltp.parse_task_headers(text).headers
    task: dict = {"id": Path(task_file).stem}
    for key in ROUTING_KEYS:
        value = headers.get(key)
        if value:
            task[key] = str(value).strip()
    return task


def classify(task: dict, roster) -> tuple[int, list]:
    """(exit code, targets) without delivering anything.

    The roster is passed in, never loaded here: classification and delivery
    must decide against ONE version, and two loads are two decisions.
    """
    if roster is None:
        # Take, never decline: the run refuses and the core answers by fallback,
        # so an unreadable file never picks a worker.
        return TAKE, []
    targets = pr.targets_for(roster, task.get("channel_id") or task.get("source") or "",
                             task.get("requested_worker"))
    # One question only: is every target on the roster? Anything else -- no
    # binding, a name never created -- is the core's, which is a real recipient.
    if targets == [pr.CORE] or pr.unknown_targets(roster, targets):
        return DECLINE, targets
    # Liveness is deliberately NOT asked: the sentinel is durable, so a worker
    # that starts later finds its work.
    return TAKE, targets


def _ws(workspace) -> Path:
    return Path(workspace) if workspace is not None else resolve_workspace()


def _log(workspace, line: str) -> None:
    """The watcher keeps only the exit code; the reason has to be kept here."""
    try:
        d = _ws(workspace) / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "pool-route-handler.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + line.rstrip() + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--task-file", default=None)
    p.add_argument("--workspace", default=None)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--parked", metavar="TASK_ID", default=None)
    p.add_argument("--retry-pass", action="store_true")
    p.add_argument("--retry-limit", type=int, default=RETRY_LIMIT)
    for ignored in ("--runtime", "--results-dir", "--repo"):
        p.add_argument(ignored, default=None)
    args, _unknown = p.parse_known_args(argv)

    ws = args.workspace
    if args.parked:
        try:
            waiting = parked(ws, args.parked)
        except MarkerStoreUnreadable as e:
            # Asked once per task on every Stop: a store this process cannot read
            # is not "nobody parked it", and the caller must be able to tell.
            print(f"pool-route-handler: --parked {args.parked}: {e}", file=sys.stderr)
            return PARKED_UNKNOWN
        return 0 if waiting else 1
    if args.retry_pass:
        print(json.dumps(retry_pass(ws, limit=args.retry_limit)))
        return 0
    if not args.task_file:
        p.error("--task-file is required unless --retry-pass or --parked")
    roster = pr.load_roster(ws)
    if not args.probe:
        # The production retry driver: the event that woke this run is what
        # re-delivers what an earlier run deferred. A crash here routes anyway.
        try:
            retry_pass(ws, roster, limit=args.retry_limit)
        except Exception:
            _log(ws, "retry pass crashed:\n" + traceback.format_exc())
    try:
        task = read_task(args.task_file)
    except FileNotFoundError:
        # Finished and archived between the probe and this run: nothing to route.
        _log(ws, f"{args.task_file}: gone before the run; nothing to route")
        return 0
    code, _targets = classify(task, roster)
    if args.probe:
        return code
    version = roster.get("version") if roster else None
    _log(ws, f"{task.get('id')}: run classify={code} targets={_targets} "
             f"roster=v{version}")
    if code == DECLINE:
        if not task.get("channel_id"):
            _log(ws, f"{task.get('id')}: no channel_id header — the core takes it; "
                     "routing metadata is never read from a task body")
        return DECLINE

    return _deliver(ws, task, roster)


def retry_dir(workspace) -> Path:
    return _ws(workspace) / "state" / "pool-route-retry"


class MarkerStoreUnreadable(Exception):
    """A store exists but cannot be read, so "not parked" is not an answer."""


def retry_dirs(workspace) -> list[Path]:
    """Every store a marker may live in, primary first.

    The second shares no component with the first below the workspace, so one
    obstructed directory cannot take automatic retry offline; it is hidden, so
    the `tasks/*.txt` glob every producer and consumer uses never sees it.
    """
    return [retry_dir(workspace), _ws(workspace) / "tasks" / ".pool-route-retry"]


def _drop_marker(ws, task_id: str) -> None:
    for d in retry_dirs(ws):
        try:
            (d / task_id).unlink()
        except OSError:
            pass


def _marker_states(workspace, task_id: str) -> tuple:
    """(present, unreadable-stores) across every store — one scan, two readers.

    `os.stat`, not `Path.exists`: a store that cannot be read must stay
    distinguishable from an absent marker, which newer `exists()` flattens.
    A store obstructed by a FILE is a negative rather than an unknown — nothing
    can be stored under it — so only a real read failure makes the answer blind.
    """
    present, blind = False, []
    for d in retry_dirs(workspace):
        try:
            os.stat(d / task_id)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            blind.append(f"{d}: {type(e).__name__}: {e}")
            continue
        present = True
    return present, blind


def parked(workspace, task_id: str) -> bool:
    """Is this task waiting for a retry pass, under ANY store?

    A marker found answers even when another store is blind; nothing found while
    a store is unreadable does not — that is the case that must not read False.
    """
    present, blind = _marker_states(workspace, task_id)
    if not present and blind:
        raise MarkerStoreUnreadable("; ".join(blind))
    return present


def _defer(ws, task_id: str, reason: str) -> int:
    """A failed delivery keeps the task the worker's: mark it for the retry
    pass and exit 0 so the watcher never hands it to the core.

    Settled means RECOVERABLE, not merely non-zero-free: exit 0 is returned only
    once a store `retry_pass` enumerates holds the marker.
    """
    failures = []
    for d in retry_dirs(ws):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / task_id).write_text(time.strftime("%Y-%m-%dT%H:%M:%S ") + reason + "\n")
        except OSError as e:
            failures.append(f"{d} ({e})")
            continue
        if failures:
            _log(ws, f"{task_id}: primary marker store unusable: {'; '.join(failures)}")
        _log(ws, f"{task_id}: deferred for retry under {d}: {reason}")
        return 0
    # Nothing is discoverable now: 0 would settle work no pass can find, and any
    # OTHER non-zero takes the watcher's fallback and gives the core the task.
    _log(ws, f"{task_id}: deferred WITHOUT marker ({'; '.join(failures)}): {reason}")
    print(f"{UNMARKED_NOTICE} {task_id}: no retry marker under "
          f"{'; '.join(failures)}; automatic retry is OFF for this task until one "
          f"of those paths is writable — exiting {UNSETTLED} so the watcher keeps "
          "its claim and does NOT hand the task to the core",
          file=sys.stderr)
    return UNSETTLED


def _deliver(ws, task: dict, roster) -> int:
    task_id = task.get("id") or "?"
    if roster is None:
        return _defer(ws, task_id, "refused: roster is absent or unreadable")
    try:
        out = rt.route(ws, task, roster)
    except rt.RouterRefused as e:
        return _defer(ws, task_id, f"refused: {e}")
    except Exception:
        _log(ws, f"{task_id}: crashed:\n" + traceback.format_exc())
        return _defer(ws, task_id, "crashed (traceback logged)")
    if out.get("failed"):
        # Unreachable via the probe, which declines these; kept so a direct
        # caller cannot turn an unknown name into a delivery.
        print(json.dumps(out), file=sys.stderr)
        return DECLINE
    _drop_marker(ws, task_id)
    _log(ws, f"{task_id}: delivered={out.get('delivered')} "
             f"already={out.get('already')} skipped={out.get('skipped')} -> 0")
    return 0


def _markers(ws) -> list:
    """Markers across every store, one per task id, oldest first. A store that
    is missing or obstructed contributes nothing rather than ending the pass."""
    seen, out = set(), []
    for d in retry_dirs(ws):
        try:
            entries = sorted(d.iterdir()) if d.is_dir() else []
        except OSError:
            entries = []
        for m in entries:
            if m.name not in seen and m.is_file():
                seen.add(m.name)
                out.append(m)
    return sorted(out, key=lambda m: (m.stat().st_mtime, m.name))


def retry_pass(ws, roster=_UNSET, limit: int = RETRY_LIMIT) -> dict:
    """Re-deliver marked tasks whose payload is still in tasks/, oldest first.

    Bounded by `limit`; the same roster the caller classified against is used,
    so one run never re-delivers against a version it did not report.
    """
    if roster is _UNSET:
        roster = pr.load_roster(ws)
    outcome = {"delivered": [], "still_deferred": [], "gone": [], "held_over": 0}
    markers = _markers(ws)
    outcome["held_over"] = max(0, len(markers) - limit)
    for marker in markers[:limit]:
        payload = _ws(ws) / "tasks" / f"{marker.name}.txt"
        if not payload.is_file():
            _drop_marker(ws, marker.name)
            outcome["gone"].append(marker.name)
            _log(ws, f"retry {marker.name}: payload gone; marker dropped")
            continue
        _deliver(ws, read_task(str(payload)), roster)
        # Tolerant on purpose: a blind store holds nothing this pass can act on,
        # and raising here would end a pass over the markers it CAN read.
        state = "still_deferred" if _marker_states(ws, marker.name)[0] else "delivered"
        outcome[state].append(marker.name)
        _log(ws, f"retry {marker.name}: {state}")
    _log(ws, f"retry pass: {json.dumps(outcome)}")
    return outcome


def _workspace_arg(argv) -> "str | None":
    args = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(args):
        if a == "--workspace" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--workspace="):
            return a.split("=", 1)[1]
    return None


def run(argv=None) -> int:
    """Every exit of the real run leaves a log line: the watcher keeps only
    the code, and a silent non-zero exit cannot be diagnosed afterwards."""
    try:
        rc = main(argv)
    except SystemExit:
        raise
    except BaseException:
        _log(_workspace_arg(argv), "unhandled:\n" + traceback.format_exc())
        raise
    given = list(sys.argv[1:] if argv is None else argv)
    if not any(a == f or a.startswith(f + "=") for a in given for f in READ_ONLY_FLAGS):
        # Logged AFTER main returns so a process exit that disagrees with this
        # line is provably outside the handler's own code.
        _log(_workspace_arg(argv), f"run returning rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(run())

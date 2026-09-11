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
                            the worker's under state/pool-route-retry/<id> and the
                            run still exits 0 -- the core repairs delivery, it never
                            answers a worker's task (owner rule). `--retry-pass`
                            re-delivers every marked task.

Run: called by src/watch-tasks-stream.sh; see dispatch_task there.
"""
from __future__ import annotations

import argparse
import json
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

# Every one of these selects a recipient, so every one is header-only.
ROUTING_KEYS = ("id", "channel_id", "source", "access_tier", "requested_worker")


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


def classify(workspace, task: dict) -> tuple[int, list]:
    """(exit code, targets) without delivering anything."""
    roster = pr.load_roster(workspace)
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
    p.add_argument("--retry-pass", action="store_true")
    for ignored in ("--runtime", "--results-dir", "--repo"):
        p.add_argument(ignored, default=None)
    args, _unknown = p.parse_known_args(argv)

    ws = args.workspace
    if args.retry_pass:
        print(json.dumps(retry_pass(ws)))
        return 0
    if not args.task_file:
        p.error("--task-file is required unless --retry-pass")
    try:
        task = read_task(args.task_file)
    except FileNotFoundError:
        # Finished and archived between the probe and this run: nothing to route.
        _log(ws, f"{args.task_file}: gone before the run; nothing to route")
        return 0
    code, _targets = classify(ws, task)
    if args.probe:
        return code
    _log(ws, f"{task.get('id')}: run classify={code} targets={_targets}")
    if code == DECLINE:
        if not task.get("channel_id"):
            _log(ws, f"{task.get('id')}: no channel_id header — the core takes it; "
                     "routing metadata is never read from a task body")
        return DECLINE

    return _deliver(ws, task)


def retry_dir(workspace) -> Path:
    return _ws(workspace) / "state" / "pool-route-retry"


def _defer(ws, task_id: str, reason: str) -> int:
    """A failed delivery keeps the task the worker's: mark it for the retry
    pass and exit 0 so the watcher never hands it to the core."""
    try:
        d = retry_dir(ws)
        d.mkdir(parents=True, exist_ok=True)
        (d / task_id).write_text(time.strftime("%Y-%m-%dT%H:%M:%S ") + reason + "\n")
    except OSError as e:
        # The marker is a convenience for the retry pass; its absence must not
        # turn into a non-zero exit that hands the task to the core.
        _log(ws, f"{task_id}: deferred WITHOUT marker ({e}): {reason}")
        return 0
    _log(ws, f"{task_id}: deferred for retry: {reason}")
    return 0


def _deliver(ws, task: dict) -> int:
    task_id = task.get("id") or "?"
    try:
        out = rt.route(ws, task, None)
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
    try:
        (retry_dir(ws) / task_id).unlink()
    except OSError:
        pass
    _log(ws, f"{task_id}: delivered={out.get('delivered')} "
             f"already={out.get('already')} skipped={out.get('skipped')} -> 0")
    return 0


def retry_pass(ws) -> dict:
    """Re-deliver every marked task whose payload is still in tasks/."""
    outcome = {"delivered": [], "still_deferred": [], "gone": []}
    d = retry_dir(ws)
    for marker in sorted(d.iterdir()) if d.is_dir() else []:
        payload = _ws(ws) / "tasks" / f"{marker.name}.txt"
        if not payload.is_file():
            marker.unlink()
            outcome["gone"].append(marker.name)
            continue
        _deliver(ws, read_task(str(payload)))
        outcome["delivered" if not marker.exists() else "still_deferred"].append(marker.name)
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
    if "--probe" not in (sys.argv[1:] if argv is None else argv):
        # Logged AFTER main returns so a process exit that disagrees with this
        # line is provably outside the handler's own code.
        _log(_workspace_arg(argv), f"run returning rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(run())

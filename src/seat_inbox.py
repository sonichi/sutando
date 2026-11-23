#!/usr/bin/env python3
"""Which seat an inbox belongs to, judged from the inbox itself and the workspace's records.

A registered seat inbox is the core's `<workspace>/tasks`, or a rostered (not retired) worker's
`<workspace>/deliveries/<id>` or own tasks folder (`connections.own_worker_homes`). Anything else, a
scratch or test inbox included, is no seat: nothing about it should reach the owner.

`seat(inbox, workspace)` is {"seat": "core"|"worker"|"", "worker": id or "", "label": text or ""};
the label is "the core" or "worker <first 8 of its id>", never a path.
CLI: seat_inbox.py seat --inbox D [--workspace W]   one line, tab-separated: seat, worker, label;
     "none" when the inbox is no registered seat (an unreadable record included).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roster_membership  # noqa: E402

NONE = {"seat": "", "worker": "", "label": ""}


def _real(path) -> str:
    return os.path.realpath(os.fspath(path))


def _rostered(workspace: Path) -> set:
    try:
        rows = json.loads((workspace / "state" / "roster.json").read_text(encoding="utf-8")).get("workers")
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(rows, dict):
        return set()
    return roster_membership.members(rows)


def _worker(wid: str) -> dict:
    return {"seat": "worker", "worker": wid, "label": f"worker {wid[:8]}"}


def seat(inbox, workspace) -> dict:
    if not inbox or not workspace:
        return dict(NONE)
    ws = Path(workspace)
    here = _real(inbox)
    if here == _real(ws / "tasks"):
        return {"seat": "core", "worker": "", "label": "the core"}
    workers = _rostered(ws)
    parent = os.path.dirname(here)
    if parent == _real(ws / "deliveries") and os.path.basename(here) in workers:
        return _worker(os.path.basename(here))
    try:
        from connections import own_worker_homes
        homes = own_worker_homes(ws)
    except Exception:  # noqa: BLE001 - an unreadable record registers nothing
        homes = []
    for home in homes:
        if home["worker"] in workers and _real(home["tasks"]) == here:
            return _worker(home["worker"])
    return dict(NONE)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="which seat an inbox belongs to")
    ap.add_argument("cmd", choices=("seat",))
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--workspace")
    a = ap.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = resolve_workspace()
    s = seat(a.inbox, ws)
    print("\t".join((s["seat"], s["worker"], s["label"])) if s["seat"] else "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())

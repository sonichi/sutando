#!/usr/bin/env python3
"""What the pool tells the broker, so the worker picker can show it.

The picker reads two different things and they arrive by different routes:

    POST /v1/workers            live/dead ids  -> each row's STATUS
    PUT  /v1/agents/<me>/profile workers meta  -> each row's LABEL and runtime

Send only one and the picker is half-right: statuses with raw 32-hex ids for
names, or names attached to nothing. So both bodies are built here, from the
one roster, and a caller sends them together.

This module builds; it does not send. The transport, its credentials and its
retry policy belong to whatever already talks to the broker.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import tempfile
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_roster as pr  # noqa: E402

# The broker's field names are the affinity era's; the roster is this design's.
# Translating here keeps that vocabulary out of everything else.
ACTIVE_STATES = ("live",)
DEAD_STATES = ("abandoned",)


def _rows(roster: dict) -> dict:
    workers = roster.get("workers")
    return workers if isinstance(workers, dict) else {}


def bindings(roster: dict) -> dict:
    """The roster's room -> worker bindings in the broker's row shape, so a
    picker can show which worker a room is pinned to."""
    out = {}
    for room, wid in (roster.get("bindings") or {}).items():
        if isinstance(wid, str) and wid:
            out[str(room)] = {"instance": wid, "instances": [wid], "pinned": True}
    return out


def snapshot(roster: dict, now=None) -> dict:
    """The `POST /v1/workers` body: which workers are running.

    `recovering` is deliberately neither active nor dead — the broker renders
    an unlisted worker as "available", which is what a restarting worker is.
    """
    rows = _rows(roster)
    live, dead = [], []
    for wid, row in sorted(rows.items()):
        state = (row or {}).get("state")
        if state in ACTIVE_STATES:
            live.append(wid)
        elif state in DEAD_STATES:
            dead.append(wid)
    return {"ts": int(now if now is not None else time.time()),
            "live_cores": live, "dead_cores": dead,
            "bindings": bindings(roster),
            "roster_version": roster.get("version")}


def profile_workers(roster: dict) -> dict:
    """The profile's `workers` map: what each worker is CALLED.

    Retired workers are omitted from both bodies — the broker treats any id it
    has metadata for as at least "available", so leaving one here would keep a
    deleted worker on the picker forever.
    """
    out = {}
    for wid, row in sorted(_rows(roster).items()):
        row = row or {}
        if row.get("state") == "retired":
            continue
        meta = {"label": str(row.get("label") or wid)}
        if row.get("runtime"):
            meta["runtime"] = str(row["runtime"])
        out[wid] = meta
    return out


def advertisement(workspace, now=None) -> dict:
    """Both bodies from one roster read, so they cannot disagree."""
    roster = pr.load_roster(workspace)
    if roster is None:
        raise FileNotFoundError("no roster to advertise")
    return {"workers_snapshot": snapshot(roster, now),
            "profile_patch": {"workers": profile_workers(roster)}}


ADVERTISEMENT_FILE = "pool-advertisement.json"


def advertisement_path(workspace) -> Path:
    """Beside the roster: the bridge reads this file and sends the two bodies."""
    return pr.roster_path(workspace).parent / ADVERTISEMENT_FILE


def write_advertisement(workspace, now=None) -> Path:
    """Persist the advertisement for the bridge: `workers` is the exact
    POST /v1/workers body, `profile_workers` the profile card's workers field.
    Written whole-or-not (temp file + replace) so a reader never sees a torn file."""
    ad = advertisement(workspace, now)
    path = advertisement_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"ts": ad["workers_snapshot"]["ts"],
                       "workers": ad["workers_snapshot"],
                       "profile_workers": ad["profile_patch"]["workers"]},
                      indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(prefix=".pool-advertisement.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body + "\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build the pool's broker advertisement")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--write", action="store_true",
                    help="also write state/pool-advertisement.json for the bridge")
    a = ap.parse_args(argv)
    try:
        ad = advertisement(a.workspace)
        if a.write:
            print(write_advertisement(a.workspace), file=sys.stderr)
        print(json.dumps(ad, indent=2))
    except FileNotFoundError as e:
        print(f"pool-advertise: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

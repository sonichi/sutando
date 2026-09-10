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
import json
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build the pool's broker advertisement")
    ap.add_argument("--workspace", default=None)
    a = ap.parse_args(argv)
    try:
        print(json.dumps(advertisement(a.workspace), indent=2))
    except FileNotFoundError as e:
        print(f"pool-advertise: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

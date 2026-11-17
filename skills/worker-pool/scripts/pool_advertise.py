#!/usr/bin/env python3
"""What the pool tells the broker, so the worker picker can show it.

The picker reads two different things and they arrive by different routes:

    POST /v1/workers            live/dead ids  -> each row's STATUS, AGENT_MXID
    PUT  /v1/agents/<me>/profile workers meta  -> each row's LABEL and runtime

Send only one and the picker is half-right: statuses with raw 32-hex ids for
names, or names attached to nothing. So both bodies are built here, from the
one roster, and a caller sends them together.

A worker's own `agent_mxid` rides in the SAME report the broker stores as the
`/v1/workers` snapshot — never the profile patch, the one place it's read back.

This module builds; it does not send. The transport, its credentials and its
retry policy belong to whatever already talks to the broker.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import tempfile
import os
import sys
import time
from pathlib import Path

# Sibling skill scripts resolve from this directory; core helpers from the repo
# root (parents[3] of skills/<name>/scripts/<file>.py, symlinks resolved).
_SCRIPTS = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS), str(_SCRIPTS.parents[2] / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import connections  # noqa: E402
import pool_roster as pr  # noqa: E402

# The broker's field names are the affinity era's; the roster is this design's.
# Translating here keeps that vocabulary out of everything else.
ACTIVE_STATES = ("live",)
# Not answering now: a recovering worker is shown as such, never as "available".
DEAD_STATES = ("abandoned", "recovering")
# report()'s default: no identity lookup, so a roster naming no real install
# is reported as before.
_NO_WORKSPACE = object()


def _rows(roster: dict) -> dict:
    workers = roster.get("workers")
    return workers if isinstance(workers, dict) else {}


def bindings(roster: dict) -> dict:
    """The roster's room -> worker bindings in the broker's row shape, so a
    picker can show which worker a room is pinned to. A binding is a worker id
    or a one-member list, the two forms the roster accepts; a binding to a
    retired worker is not advertised, since retired ids occur nowhere."""
    rows = _rows(roster)
    out = {}
    for room, bound in (roster.get("bindings") or {}).items():
        members = [m for m in (bound if isinstance(bound, list) else [bound])
                   if isinstance(m, str) and m]
        members = [m for m in members if (rows.get(m) or {}).get("state") != "retired"]
        if members:
            out[str(room)] = {"instance": members[0], "instances": members, "pinned": True}
    return out


def _labels(roster: dict) -> dict:
    """What each non-retired worker is called here, as applied by this runtime."""
    out = {}
    for wid, row in sorted(_rows(roster).items()):
        row = row or {}
        if row.get("state") == "retired":
            continue
        out[wid] = str(pr.display_label(row, wid))
    return out


def _worker_agent_mxid(wid: str, workspace) -> str:
    """`wid`'s own agent id; empty for a disabled or parked identity row.
    Reads `agent_mxid` only, never `token_ref`."""
    row = connections.worker_enabled_identity(wid, workspace)
    return str((row or {}).get("agent_mxid") or "").strip()


def report(roster: dict, now=None, workspace=_NO_WORKSPACE) -> dict:
    """Runtime facts the broker calls `applied`, never names or bindings.
    With `workspace`, each worker's own `agent_mxid` rides along too."""
    workers = []
    for wid, row in sorted(_rows(roster).items()):
        row = row or {}
        w = {"id": wid, "state": str(row.get("state"))}
        if row.get("runtime"):
            w["runtime"] = str(row["runtime"])
        if workspace is not _NO_WORKSPACE:
            mxid = _worker_agent_mxid(wid, workspace)
            if mxid:
                w["agent_mxid"] = mxid
        workers.append(w)
    return {"ts": int(now if now is not None else time.time()),
            "roster_version": roster.get("version"),
            "workers": workers,
            "applied": {"config_version": roster.get("config_version"),
                        "labels": _labels(roster),
                        "bindings": bindings(roster)}}


def snapshot(roster: dict, now=None) -> dict:
    """The `POST /v1/workers` body today's broker accepts, projected from the
    report: live ids, ids not answering (abandoned or recovering), bindings."""
    rep = report(roster, now)
    live = [w["id"] for w in rep["workers"] if w["state"] in ACTIVE_STATES]
    dead = [w["id"] for w in rep["workers"] if w["state"] in DEAD_STATES]
    return {"ts": rep["ts"], "live_cores": live, "dead_cores": dead,
            "bindings": rep["applied"]["bindings"],
            "roster_version": rep["roster_version"]}


def profile_workers(roster: dict) -> dict:
    """The profile card's `workers` map today's broker accepts, projected from
    the report's applied labels. Retired workers are omitted — the broker treats
    any id it has metadata for as at least "available"."""
    rep = report(roster)
    runtime = {w["id"]: w.get("runtime") for w in rep["workers"]}
    out = {}
    for wid, label in rep["applied"]["labels"].items():
        meta = {"label": label}
        if runtime.get(wid):
            meta["runtime"] = runtime[wid]
        out[wid] = meta
    return out


def advertisement(workspace, now=None) -> dict:
    """Both bodies from one roster read, so they cannot disagree. `report`
    carries each worker's `agent_mxid` — the body sent to `POST /v1/workers`."""
    roster = pr.load_roster(workspace)
    if roster is None:
        raise FileNotFoundError("no roster to advertise")
    return {"report": report(roster, now, workspace),
            "workers_snapshot": snapshot(roster, now),
            "profile_patch": {"workers": profile_workers(roster)}}


ADVERTISEMENT_FILE = "pool-advertisement.json"


def advertisement_path(workspace) -> Path:
    """Beside the roster: the bridge reads this file and sends the two bodies."""
    return pr.roster_path(workspace).parent / ADVERTISEMENT_FILE


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def write_advertisement(workspace, now=None) -> Path:
    """Persist the advertisement for the bridge: `report` is the reported leg,
    `workers` the exact POST /v1/workers body, `profile_workers` the profile
    card's workers field. Written whole-or-not (temp file + replace) so a
    reader never sees a torn file, and under a lock from the roster read to
    the replace: the roster is re-read under it, so every writer publishes the
    roster as it is now, and the derived file follows the roster even backwards."""
    path = advertisement_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(path), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            ad = advertisement(workspace, now)
            body = json.dumps({"ts": ad["workers_snapshot"]["ts"],
                               "report": ad["report"],
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
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return path


def ensure_advertisement(workspace, now=None):
    """Write the advertisement when the file does not say what the roster says
    now: missing, an older roster, or an older schema (a file without the
    report, or whose report differs from the roster's). None when there is no
    roster. For boot, where a compile may have happened while nothing was
    running to advertise it; cheap to call every run."""
    if not pr.roster_path(workspace).exists():
        return None
    path = advertisement_path(workspace)
    try:
        have = _projection(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        have = None
    ad = advertisement(workspace, now=0)
    want = _projection({"report": ad["report"], "workers": ad["workers_snapshot"],
                        "profile_workers": ad["profile_patch"]["workers"]})
    if have == want:
        return path
    return write_advertisement(workspace, now=now)


def _projection(doc: dict) -> dict:
    """Every half the file publishes, timestamps removed: a stale half that the
    report does not mention must still force a rewrite."""
    def strip_ts(v):
        return {k: x for k, x in v.items() if k != "ts"} if isinstance(v, dict) else v
    return {"report": strip_ts(doc["report"]), "workers": strip_ts(doc["workers"]),
            "profile_workers": doc["profile_workers"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="build the pool's broker advertisement")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--write", action="store_true",
                    help="also write state/pool-advertisement.json for the bridge")
    ap.add_argument("--ensure", action="store_true",
                    help="write the file only if the roster has moved past it; no roster is not an error")
    a = ap.parse_args(argv)
    if a.ensure:
        path = ensure_advertisement(a.workspace)
        print(path if path else "pool-advertise: no roster, nothing to advertise", file=sys.stderr)
        return 0
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

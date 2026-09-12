#!/usr/bin/env python3
"""Bindings the owner writes; a roster the core compiles; the router only reads.

Two files with different authors, which is the whole point:

    state/bindings.json   owner-authored — one durable addressee per line of work
    state/roster.json     compiled by the core from workers + bindings + states

The router's entire input is the roster and one task. Keeping compilation here
and out of the router is what makes routing testable by replay: same roster,
same task, same deliveries, with no clock, no directory listing and no liveness
probe in the decision.

A binding is keyed on the SOURCE of work — the raw channel id the bridge stamps
(`!abc:ag2.space`) — never on a task property, because the owner declares it
before any task exists.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Skill script: reach the core's workspace resolver in src/ (repo root is
# parents[3] of skills/<name>/scripts/<file>.py, symlinks resolved).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workspace_default import resolve_workspace  # noqa: E402

WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CORE = "core"

# The states the design gives the router a rule for; anything else is a typo we
# refuse rather than silently treat as not-live.
STATES = ("live", "recovering", "abandoned", "retired")


class RosterError(Exception):
    """A declaration the roster cannot represent, refused at compile time."""


def _root(workspace) -> Path:
    return Path(workspace) if workspace is not None else resolve_workspace()


def bindings_path(workspace) -> Path:
    return _root(workspace) / "state" / "bindings.json"


def roster_path(workspace) -> Path:
    return _root(workspace) / "state" / "roster.json"


def _lock_path(workspace) -> Path:
    p = roster_path(workspace)
    return p.with_name(p.name + ".lock")


@contextlib.contextmanager
def _locked(workspace):
    """Exclusive lock over one read-merge-write transaction on the roster.

    Two `register_worker` calls that each read before either writes end with
    only the later write surviving; the lock closes that window.
    """
    path = _lock_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_bindings(workspace) -> dict:
    """Absent is a valid empty declaration. Unreadable or mis-shaped is refused:
    read as empty, a corrupt file compiles into a roster that sends every bound
    task to the core."""
    p = bindings_path(workspace)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RosterError(f"bindings unreadable, keeping the last roster: {p}: {e}")
    bindings = raw.get("bindings", {}) if isinstance(raw, dict) else None
    if not isinstance(bindings, dict):
        raise RosterError(f"bindings must be an object under 'bindings': {p}")
    return bindings


def save_bindings(workspace, bindings: dict) -> None:
    """Persist the owner's declaration in the shape `load_bindings` reads."""
    _write_atomic(bindings_path(workspace), {"bindings": dict(bindings)})


def load_roster(workspace):
    """`None` when absent or unreadable. The router must refuse the pass on
    None rather than default to the core — a silent default routes every task
    to one recipient the moment the file is unwritable."""
    raw = _read(roster_path(workspace), None)
    if not isinstance(raw, dict) or "workers" not in raw:
        return None
    return raw


def _load_existing_roster_strict(workspace):
    """The writer's own read of the roster it is about to replace.

    Absent is a valid starting point. Unreadable or malformed is not — treating
    either as absent would silently overwrite whatever it is hiding.
    """
    p = roster_path(workspace)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise RosterError(f"roster unreadable, refusing to touch it: {p}: {e}") from e
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise RosterError(f"roster is not valid JSON, refusing to touch it: {p}: {e}") from e
    if not isinstance(raw, dict) or "workers" not in raw:
        raise RosterError(f"roster is missing 'workers', refusing to touch it: {p}")
    return raw


def resolve_label(roster: dict, name: str) -> str:
    """A worker's display label to its id; anything else unchanged.

    An unknown or AMBIGUOUS label is returned as given, so the caller fails the
    task by that name instead of picking one of the workers that share it.
    """
    workers = roster.get("workers") or {}
    if name in workers or name == CORE:
        return name
    hits = [wid for wid, row in workers.items() if (row or {}).get("label") == name]
    return hits[0] if len(hits) == 1 else name


def targets_for(roster: dict, source: str, requested_worker=None) -> list:
    """Resolve one task to its recipients: `requested_worker`, else the binding
    for its source, else the core. A set resolves to its member list.

    An envelope names a worker the way a person does — by label — while the
    roster is keyed by id, so a requested worker is resolved before use.
    """
    if requested_worker:
        return [resolve_label(roster, requested_worker)]
    bound = (roster.get("bindings") or {}).get(source)
    if bound is None:
        return [CORE]
    return list(bound) if isinstance(bound, list) else [bound]


def unknown_targets(roster: dict, targets) -> list:
    """Targets absent from the roster. The router fails such a task by name
    rather than substituting a reachable recipient."""
    known = set(roster.get("workers") or {}) | {CORE}
    return [t for t in targets if t not in known]


def compile_roster(workspace, workers: dict, bindings=None, version=None) -> dict:
    """Build the roster the router reads. Refuses declarations it cannot honour
    rather than emitting a roster that routes somewhere unintended."""
    bindings = dict(bindings if bindings is not None else load_bindings(workspace))
    for wid, row in (workers or {}).items():
        if wid != CORE and not WORKER_ID_RE.match(wid):
            raise RosterError(f"worker id must match {WORKER_ID_RE.pattern!r}: {wid!r}")
        state = (row or {}).get("state")
        if state not in STATES:
            raise RosterError(f"worker {wid!r} has state {state!r}; expected one of {STATES}")

    known = set(workers or {}) | {CORE}
    for source, bound in bindings.items():
        members = list(bound) if isinstance(bound, list) else [bound]
        if not members:
            raise RosterError(f"binding {source!r} names no target")
        if len(members) > 1:
            # Members would share one payload and one result path; the first to
            # finish archives the other's work. Refused until members get their own.
            raise RosterError(f"binding {source!r} names {len(members)} targets; "
                              "fan-out is not supported yet")
        missing = [m for m in members if m not in known]
        if missing:
            raise RosterError(
                f"binding {source!r} names {missing} which are not workers — "
                "a binding to a nonexistent target fails every task from that source")

    prev = _load_existing_roster_strict(workspace) or {}
    roster = {"version": version if version is not None else int(prev.get("version", 0)) + 1,
              "compiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "workers": dict(workers or {}), "bindings": bindings}
    _write_atomic(roster_path(workspace), roster)
    return roster


def register_worker(workspace, worker_id: str, label: str, room=None) -> dict:
    """Add a worker to the roster and, if given, bind its room — the one
    production writer for this transaction.

    Read-merge-write (existing workers, then bindings, then both durable
    publications) runs under `_locked` so two callers registering different
    workers at once cannot each read before either writes, which is what
    drops one of them from the result.
    """
    with _locked(workspace):
        workers = dict((_load_existing_roster_strict(workspace) or {}).get("workers") or {})
        workers[worker_id] = {"state": "live", "label": label or worker_id}
        bindings = dict(load_bindings(workspace))
        if room:
            bindings[room] = worker_id
            # The next registration reloads bindings.json, not the roster: a
            # binding held only in the compiled roster is discarded by it.
            save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)

#!/usr/bin/env python3
"""Bindings the owner writes; a roster the core compiles; the router only reads.

Two files with different authors, which is the whole point:

    state/bindings.json   owner-authored — one durable addressee per line of work
    state/roster.json     compiled by the core from workers + bindings + states

The router's entire input is the roster and one task. Keeping compilation here
and out of the router is what makes routing testable by replay: same roster,
same task, same deliveries, with no clock, no directory listing and no liveness
probe in the decision.

A binding is keyed on the SOURCE of work (`room:!abc:ag2.space`), never on a
task property, because the owner declares it before any task exists.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    return _read(bindings_path(workspace), {}).get("bindings", {})


def load_roster(workspace):
    """`None` when absent or unreadable. The router must refuse the pass on
    None rather than default to the core — a silent default routes every task
    to one recipient the moment the file is unwritable."""
    raw = _read(roster_path(workspace), None)
    if not isinstance(raw, dict) or "workers" not in raw:
        return None
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
        missing = [m for m in members if m not in known]
        if missing:
            raise RosterError(
                f"binding {source!r} names {missing} which are not workers — "
                "a binding to a nonexistent target fails every task from that source")

    prev = load_roster(workspace) or {}
    roster = {"version": version if version is not None else int(prev.get("version", 0)) + 1,
              "compiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "workers": dict(workers or {}), "bindings": bindings}
    _write_atomic(roster_path(workspace), roster)
    return roster

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

sys.path.insert(0, str(Path(__file__).resolve().parent))
# helpers live in the core; repo root is parents[3] from this directory
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from workspace_default import resolve_workspace  # noqa: E402

WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CORE = "core"

# A bound SET is compiled to ONE name under this prefix, which WORKER_ID_RE can
# never produce, so a reader that predates sets resolves it to nothing.
SET_PREFIX = "set:"
SET_SEP = "+"

# The states the design gives the router a rule for; anything else is a typo we
# refuse rather than silently treat as not-live.
STATES = ("live", "recovering", "abandoned", "retired")


class RosterError(Exception):
    """A declaration the roster cannot represent, refused at compile time."""


class PublishError(OSError):
    """The roster was written but its advertisement was not: the router will
    follow the new roster, the picker will not until a publish succeeds."""

    def __init__(self, roster: dict, cause: OSError):
        super().__init__(getattr(cause, "errno", None),
                         f"roster v{roster.get('version')} written, advertisement not: {cause}")
        self.roster = roster


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


def _lock_path(workspace) -> Path:
    return _root(workspace) / "state" / ".roster.lock"


@contextlib.contextmanager
def _locked(workspace):
    """Two `register_worker` calls that each read before either writes end
    with only the later write surviving; the lock closes that window."""
    p = _lock_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def save_bindings(workspace, bindings: dict) -> None:
    """Persist the owner's declaration in the shape `load_bindings` reads."""
    _write_atomic(bindings_path(workspace), {"bindings": dict(bindings)})


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


LEGACY_WORKER_FIELD = "target_worker"


def requested_worker_of(task: dict, warn=None) -> "str | None":
    """The route the sender asked for, from the canonical field.

    `target_worker` is accepted for one migration window and reported, so a
    producer that has not moved keeps working and is visible while it does.
    Two fields that DISAGREE name two recipients: neither is chosen, because
    picking one silently routes the owner's message somewhere they can no
    longer see. The task falls through to its binding instead.
    """
    say = warn if warn is not None else (lambda m: print(m, file=sys.stderr))
    canonical = (task.get("requested_worker") or "").strip() or None
    legacy = (task.get(LEGACY_WORKER_FIELD) or "").strip() or None
    if canonical and legacy and canonical != legacy:
        say(f"pool_roster: SECURITY: requested_worker={canonical!r} disagrees with "
            f"{LEGACY_WORKER_FIELD}={legacy!r}; ignoring both and using the binding")
        return None
    if legacy and not canonical:
        say(f"pool_roster: DEPRECATED: {LEGACY_WORKER_FIELD} is the old name for "
            f"requested_worker; the producer of this task should be updated")
        return legacy
    return canonical


def encode_set(members) -> str:
    """The compiled form of a set of two or more members.

    A list would be read as fan-out by every reader that predates this file; an
    unresolvable single name is read as a target that is not on the roster, and
    that path already sends the task to the core rather than to a worker.
    """
    return SET_PREFIX + SET_SEP.join(members)


def members_of(bound) -> list:
    """The ordered members behind one binding value, declared or compiled.

    The ONE decoder: a declaration is a name or a list, a compiled set is the
    encoded name, and nothing else may read either shape directly.
    """
    if bound is None:
        return []
    if isinstance(bound, str):
        if bound.startswith(SET_PREFIX):
            return [m for m in bound[len(SET_PREFIX):].split(SET_SEP) if m]
        return [bound]
    if isinstance(bound, (list, tuple)):
        return list(bound)
    return []


def targets_for(roster: dict, source: str, requested_worker=None) -> list:
    """Resolve one task to its recipients: `requested_worker`, else the binding
    for its source, else the core. A set resolves to ONE member: the addressed
    one, or the primary (`members[0]`) when nothing is addressed. Fan-out is not
    a routing outcome here; every task has exactly one recipient.

    An envelope names a worker the way a person does — by label — while the
    roster is keyed by id, so a requested worker is resolved before use.
    """
    members = members_of((roster.get("bindings") or {}).get(source))
    if requested_worker:
        wid = resolve_label(roster, requested_worker)
        # Addressing cannot reach past a bound set: a name outside it is unknown
        # HERE even if the roster knows it, so the router fails it by name.
        if len(members) > 1 and wid not in members:
            return [f"{wid}:not-a-member-of:{source}"]
        return [wid]
    if not members:
        return [CORE]
    # The primary answers an unaddressed task. Never the whole set (fan-out).
    return [members[0]]


def unknown_targets(roster: dict, targets) -> list:
    """Targets absent from the roster. The router fails such a task by name
    rather than substituting a reachable recipient."""
    known = set(roster.get("workers") or {}) | {CORE}
    return [t for t in targets if t not in known]


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


def validate_workers(workers) -> None:
    """The roster's type boundary, so every caller refuses the same shapes.
    A row that is not an object would otherwise surface as an AttributeError
    from whichever reader touched it first."""
    if workers is not None and not isinstance(workers, dict):
        raise RosterError(f"workers must be an object, got {type(workers).__name__}")
    for wid, row in (workers or {}).items():
        if wid != CORE and not WORKER_ID_RE.match(wid):
            raise RosterError(f"worker id must match {WORKER_ID_RE.pattern!r}: {wid!r}")
        if not isinstance(row, dict):
            raise RosterError(f"worker {wid!r} row must be an object, got {type(row).__name__}")
        state = row.get("state")
        if state not in STATES:
            raise RosterError(f"worker {wid!r} has state {state!r}; expected one of {STATES}")


def validate_current_roster(workspace) -> None:
    """Refuse a stored roster this process would fail on mid-mutation, so a
    caller validates BEFORE it writes anything rather than part-way through."""
    raw = _load_existing_roster_strict(workspace)
    if raw is not None:
        validate_workers(raw.get("workers"))


def compile_roster(workspace, workers: dict, bindings=None, version=None) -> dict:
    """Build the roster the router reads. Refuses declarations it cannot honour
    rather than emitting a roster that routes somewhere unintended."""
    declared = dict(bindings if bindings is not None else load_bindings(workspace))
    validate_workers(workers)

    known = set(workers or {}) | {CORE}
    compiled = {}
    for source, bound in declared.items():
        members = members_of(bound)
        if not members:
            raise RosterError(f"binding {source!r} names no target")
        if len(set(members)) != len(members):
            raise RosterError(f"binding {source!r} names a member twice")
        missing = [m for m in members if m not in known]
        if missing:
            raise RosterError(
                f"binding {source!r} names {missing} which are not workers — "
                "a binding to a nonexistent target fails every task from that source")
        # A set is ADDRESSED, never fanned out: one task, one recipient, one
        # result path — and it is compiled to a name no older reader resolves.
        compiled[source] = members[0] if len(members) == 1 else encode_set(members)

    prev = _load_existing_roster_strict(workspace) or {}
    roster = {"version": version if version is not None else int(prev.get("version", 0)) + 1,
              "compiled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "workers": dict(workers or {}), "bindings": compiled}
    _write_atomic(roster_path(workspace), roster)
    _publish(workspace, roster)
    return roster


def _publish(workspace, roster: dict) -> None:
    """The advertisement is derived from the roster, so it is published where the
    roster is written: a direct `bind_room` is then harmless by construction."""
    import pool_advertise as pa  # sibling; it imports this module, so bound late
    try:
        pa.write_advertisement(workspace)
    except OSError as e:
        raise PublishError(roster, e) from e


def register_worker(workspace, worker_id: str, label: str, room=None, runtime=None) -> dict:
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
        if runtime:
            workers[worker_id]["runtime"] = str(runtime)
        bindings = dict(load_bindings(workspace))
        if room:
            bindings[room] = worker_id
            # The next registration reloads bindings.json, not the roster: a
            # binding held only in the compiled roster is discarded by it.
            save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)


def bind_room(workspace, room: str, target, mode: str = "replace") -> dict:
    """Bind one room to one worker or to a COMPLETE ordered set, named by id or
    by unique label — the one production writer for a pin, and it publishes: the
    compile it ends in writes the advertisement too.

    The whole set is resolved, saved and compiled inside one hold of the lock, so
    a two-member pin never passes through a published one-member state. Same
    locked read-merge-write as `register_worker`; an unknown, ambiguous or
    repeated name is refused BEFORE the declaration is saved, so bindings.json
    never names a target the roster would reject on its next compile.
    """
    names = [target] if isinstance(target, str) else list(target or [])
    if not names or not all(isinstance(n, str) and n.strip() for n in names):
        raise RosterError(f"binding {room!r} names no target")
    if mode not in ("replace", "add"):
        raise RosterError(f"bind mode {mode!r} is not 'replace' or 'add'")
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to bind to")
        workers = dict(raw.get("workers") or {})
        wids: list = []
        for name in names:
            wid = resolve_label(raw, name.strip())
            if wid != CORE and wid not in workers:
                raise RosterError(f"binding {room!r} names {name!r}, which is not a worker")
            if wid in wids:
                raise RosterError(f"binding {room!r} names {name!r} twice")
            wids.append(wid)  # the owner's order is rank; the first is the primary
        bindings = dict(load_bindings(workspace))
        if mode == "add":
            held = members_of(bindings.get(room))
            wids = held + [w for w in wids if w not in held]
        bindings[room] = wids if len(wids) > 1 else wids[0]
        save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)


def unbind_room(workspace, room: str, worker: str | None = None) -> dict:
    """Drop a room's binding, or one member of its set. Absent is not an error:
    an unpin of an unbound room is the state the owner asked for."""
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to unbind")
        workers = dict(raw.get("workers") or {})
        bindings = dict(load_bindings(workspace))
        if worker is None:
            bindings.pop(room, None)
        else:
            members = members_of(bindings.get(room))
            wid = resolve_label(raw, worker)
            members = [m for m in members if m != wid]
            # Removing the primary promotes the next member; an emptied set unpins.
            if not members:
                bindings.pop(room, None)
            else:
                bindings[room] = members if len(members) > 1 else members[0]
        save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)

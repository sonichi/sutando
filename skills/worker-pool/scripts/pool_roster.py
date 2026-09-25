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
from util_paths import task_event_handler_config_path  # noqa: E402

WORKER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CORE = "core"

# The states the design gives the router a rule for; anything else is a typo we
# refuse rather than silently treat as not-live.
STATES = ("live", "recovering", "abandoned", "retired")
# Every state the router still delivers to; a retired worker leaves the roster.
ROUTABLE_STATES = ("live", "recovering", "abandoned")


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
    bindings = dict(bindings if bindings is not None else load_bindings(workspace))
    validate_workers(workers)

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


class HandlerPublishError(RosterError):
    """The handler could not be published, so no pool may be registered.

    core's watcher reads a missing/unusable config as the ordinary no-pool
    case, so a pool that exists without one is indistinguishable from no pool
    at all -- and worker-bound tasks would fall through to the unrestricted
    core.
    """


def publish_task_event_handler(workspace):
    """Declare this skill's router as core's task-event handler.

    Written to <workspace>/state/task-event-handler.json, which core's
    watcher also fswatches -- so a worker registration takes effect on the
    watcher's very next event, no restart. An install that never registers a
    worker never writes this, and the watcher behaves exactly as it did
    before this skill existed. `_write_atomic`'s tmp name is PID-suffixed, so
    two concurrent registrations (the caller already serializes via `_locked`,
    but this function is also exercised directly, unlocked, elsewhere) never
    collide on the same tmp path the way a shared name would.
    """
    handler = Path(__file__).resolve().parent / "pool_route_handler.py"
    cfg = task_event_handler_config_path(Path(workspace) / "state")
    try:
        _write_atomic(cfg, {"handler": str(handler)})
    except OSError as e:
        raise HandlerPublishError(
            f"cannot publish the task-event handler at {cfg}: {e}") from e
    return cfg


def ensure_task_event_handler(workspace) -> "Path | None":
    """Backfill for a pool that predates this file (register_worker() is its
    only writer, so an install that upgraded without a new registration since
    never gets it written) or whose declaration has gone stale. Republishes
    only when needed, so a healthy sweep costs one read. None when every worker
    is retired: the router targets any other state (it never asks liveness).
    """
    roster = load_roster(workspace) or {}
    workers = roster.get("workers") or {}
    if not any(isinstance(w, dict) and w.get("state") in ROUTABLE_STATES
               for w in workers.values()):
        return None
    handler = Path(__file__).resolve().parent / "pool_route_handler.py"
    cfg = task_event_handler_config_path(Path(workspace) / "state")
    current = _read(cfg, None)
    if isinstance(current, dict) and current.get("handler") == str(handler):
        return cfg
    return publish_task_event_handler(workspace)


def register_worker(workspace, worker_id: str, label: str, room=None, runtime=None) -> dict:
    """Add a worker to the roster and, if given, bind its room — the one
    production writer for this transaction.

    Read-merge-write (existing workers, then bindings, then both durable
    publications) runs under `_locked` so two callers registering different
    workers at once cannot each read before either writes, which is what
    drops one of them from the result.
    """
    with _locked(workspace):
        # Before any durable write: a registration that survived a failed publish
        # would leave a real pool the launcher cannot distinguish from no pool.
        publish_task_event_handler(workspace)
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


def bind_room(workspace, room: str, target: str) -> dict:
    """Bind one room to one worker, named by id or by a unique label — the one
    production writer for a pin, and it publishes: the compile it ends in writes
    the advertisement too, and the pin (re)publishes the task-event handler
    config, which only `register_worker` wrote before. Same locked read-merge-write as `register_worker`; an
    unknown or ambiguous name is refused BEFORE the declaration is saved, so
    bindings.json never names a target the roster would reject on its next
    compile."""
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to bind to")
        workers = dict(raw.get("workers") or {})
        wid = resolve_label(raw, target)
        if wid != CORE and wid not in workers:
            raise RosterError(f"binding {room!r} names {target!r}, which is not a worker")
        publish_task_event_handler(workspace)
        bindings = dict(load_bindings(workspace))
        bindings[room] = wid
        save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)


def rename_worker(workspace, target: str, label: str) -> "tuple[str, str, dict | None]":
    """Change one worker's display label — the one production writer for a
    rename. Returns (worker id, old label, roster), roster None on a no-op.

    The label lives only in the roster; the advertisement is derived from it
    and republished by the compile, and the id-derived session name is untouched.
    A new name another worker already answers to is refused, so `resolve_label`
    never becomes ambiguous.
    """
    new = (label or "").strip()
    if not new:
        raise RosterError("a label cannot be empty")
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to rename")
        workers = dict(raw.get("workers") or {})
        if target in workers:
            wid = target
        else:
            hits = [w for w, row in workers.items() if (row or {}).get("label") == target]
            if len(hits) > 1:
                raise RosterError(f"{target!r} is the label of {len(hits)} workers; "
                                  "name one by id")
            if not hits:
                raise RosterError(f"{target!r} is not a worker id or label")
            wid = hits[0]
        old = (workers[wid] or {}).get("label") or wid
        if new == old:
            return wid, old, None
        if new == CORE:
            raise RosterError(f"{CORE!r} names the core; a worker cannot take it")
        for other, row in workers.items():
            if other != wid and new in (other, (row or {}).get("label")):
                raise RosterError(f"{new!r} already names worker {other}")
        workers[wid] = {**(workers[wid] or {}), "label": new}
        return wid, old, compile_roster(workspace, workers, load_bindings(workspace))


def unbind_room(workspace, room: str) -> dict:
    """Drop a room's binding; its tasks go to the core again. Absent is not an
    error: an unpin of an unbound room is the state the owner asked for."""
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to unbind")
        workers = dict(raw.get("workers") or {})
        bindings = dict(load_bindings(workspace))
        bindings.pop(room, None)
        save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)

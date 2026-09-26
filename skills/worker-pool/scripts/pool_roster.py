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
MAX_DISPLAY_LABEL_LENGTH = 120
PROFILE_WORKER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RESERVED_DISPLAY_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


class RosterError(Exception):
    """A declaration the roster cannot represent, refused at compile time."""


class AmbiguousWorkerName(RosterError):
    """A requested name would select more than one recipient."""


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


def display_label(row: dict, worker_id: str) -> str:
    """The name shown to people and accepted when it identifies one worker."""
    row = row if isinstance(row, dict) else {}
    return row.get("display_label") or row.get("label") or worker_id


def resolve_label(roster: dict, name: str) -> str:
    """Resolve an exact id or core, then a unique base or display label.

    An unknown name stays unchanged for the caller's usual unknown-name policy.
    Human names shared by recipients are refused. Exact recipient names win so
    an old stored display label cannot make an id or the core unavailable.
    """
    workers = roster.get("workers") or {}
    if name in workers or name == CORE:
        return name
    hits = set()
    for wid, row in workers.items():
        if name in ((row or {}).get("label"), (row or {}).get("display_label")):
            hits.add(wid)
    if len(hits) > 1:
        raise AmbiguousWorkerName(f"{name!r} names more than one recipient")
    return next(iter(hits)) if hits else name


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
        if "display_label" in row:
            _validate_display_label(row["display_label"], wid)


def _validate_display_label(label, worker_id: str) -> str:
    if not isinstance(label, str) or not label or label != label.strip():
        raise RosterError(f"worker {worker_id!r} has an invalid display label")
    if len(label) > MAX_DISPLAY_LABEL_LENGTH or any(ord(ch) < 32 or ord(ch) == 127 for ch in label):
        raise RosterError(f"worker {worker_id!r} has an invalid display label")
    return label


def validate_current_roster(workspace) -> None:
    """Refuse a stored roster this process would fail on mid-mutation, so a
    caller validates BEFORE it writes anything rather than part-way through."""
    raw = _load_existing_roster_strict(workspace)
    if raw is not None:
        validate_workers(raw.get("workers"))


def compile_roster(workspace, workers: dict, bindings=None, version=None,
                   *, worker_label_config_version=None,
                   worker_label_profile_mxid=None) -> dict:
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
    for field in ("config_version", "worker_label_config_version", "worker_label_profile_mxid"):
        if field in prev:
            roster[field] = prev[field]
    if worker_label_profile_mxid is not None:
        if roster.get("worker_label_profile_mxid") != worker_label_profile_mxid:
            roster.pop("worker_label_config_version", None)
        roster["worker_label_profile_mxid"] = worker_label_profile_mxid
    if worker_label_config_version is not None:
        roster["worker_label_config_version"] = worker_label_config_version
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


def _ensure_advertisement(workspace, roster: dict) -> None:
    import pool_advertise as pa
    try:
        pa.ensure_advertisement(workspace)
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
        for other, row in workers.items():
            if other != worker_id and isinstance(row, dict) and worker_id in (
                    row.get("label"), row.get("display_label")):
                raise RosterError(f"worker id {worker_id!r} conflicts with worker {other}'s name")
        # Before roster or binding writes: a registration that survived a failed
        # handler publish would leave a pool the launcher cannot recognize.
        publish_task_event_handler(workspace)
        previous = workers.get(worker_id) or {}
        workers[worker_id] = {"state": "live", "label": label or worker_id}
        if isinstance(previous, dict) and "display_label" in previous:
            workers[worker_id]["display_label"] = previous["display_label"]
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
    the advertisement too. Same locked read-merge-write as `register_worker`; an
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
        bindings = dict(load_bindings(workspace))
        bindings[room] = wid
        save_bindings(workspace, bindings)
        return compile_roster(workspace, workers, bindings)


def rename_worker(workspace, target: str, label: str) -> "tuple[str, str, dict | None]":
    """Change one worker's base routing label under the roster lock.
    Returns (worker id, old label, roster), roster None on a no-op.

    A broker display-label override remains separate. The id-derived session
    name is untouched. Names another worker answers to are refused.
    """
    new = (label or "").strip()
    if not new:
        raise RosterError("a label cannot be empty")
    with _locked(workspace):
        raw = _load_existing_roster_strict(workspace)
        if raw is None:
            raise RosterError("no roster; nothing to rename")
        workers = dict(raw.get("workers") or {})
        wid = resolve_label(raw, target)
        if wid not in workers:
            raise RosterError(f"{target!r} is not a worker id or label")
        old = (workers[wid] or {}).get("label") or wid
        if new == old:
            return wid, old, None
        if new == CORE:
            raise RosterError(f"{CORE!r} names the core; a worker cannot take it")
        for other, row in workers.items():
            if other != wid and new in (other, (row or {}).get("label"),
                                       (row or {}).get("display_label")):
                raise RosterError(f"{new!r} already names worker {other}")
        workers[wid] = {**(workers[wid] or {}), "label": new}
        return wid, old, compile_roster(workspace, workers, load_bindings(workspace))


def apply_profile_label_overrides(workspace, labels: dict, config_version: int,
                                  profile_mxid: str) -> dict:
    """Apply a complete display snapshot without changing worker IDs or base labels."""
    if type(config_version) is not int or config_version < 0:
        raise RosterError("worker label config version must be a non-negative integer")
    if not isinstance(labels, dict):
        raise RosterError("worker label overrides must be an object")
    if (not isinstance(profile_mxid, str) or not profile_mxid.startswith("@")
            or ":" not in profile_mxid or profile_mxid != profile_mxid.strip()
            or len(profile_mxid) > 255
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in profile_mxid)):
        raise RosterError("worker label profile mxid is invalid")
    with _locked(workspace):
        roster = _load_existing_roster_strict(workspace)
        if roster is None:
            raise RosterError("no roster; nothing to label")
        validate_workers(roster.get("workers"))
        stored_version = roster.get("worker_label_config_version", -1)
        if type(stored_version) is not int or stored_version < -1 or (
                "worker_label_config_version" in roster and stored_version < 0):
            raise RosterError("stored worker label config version is invalid")
        stored_source = roster.get("worker_label_profile_mxid")
        if stored_source is not None and (not isinstance(stored_source, str) or not stored_source):
            raise RosterError("stored worker label profile mxid is invalid")
        source_changed = stored_source != profile_mxid
        previous = -1 if source_changed else stored_version
        if config_version < previous:
            return {"changed": False, "stale": True, "roster_version": roster["version"],
                    "worker_label_config_version": previous, "pending_worker_ids": []}

        workers = {wid: dict(row) for wid, row in roster["workers"].items()}
        pending = []
        for wid, label in labels.items():
            if not isinstance(wid, str) or not PROFILE_WORKER_ID_RE.fullmatch(wid):
                raise RosterError(f"invalid worker id in display labels: {wid!r}")
            _validate_display_label(label, wid)
            if (label == CORE or RESERVED_DISPLAY_ID_RE.fullmatch(label)
                    or (label in workers and label != wid)):
                raise RosterError(f"worker {wid!r} display label uses a reserved recipient name")
            if wid not in workers:
                pending.append(wid)
        changed = False
        for wid, row in workers.items():
            if row["state"] == "retired":
                continue
            wanted = labels.get(wid)
            if wanted is None:
                changed |= row.pop("display_label", None) is not None
            elif row.get("display_label") != wanted:
                row["display_label"] = wanted
                changed = True
        if config_version == previous:
            if not changed and not pending:
                _ensure_advertisement(workspace, roster)
                return {"changed": False, "stale": False, "roster_version": roster["version"],
                        "worker_label_config_version": previous, "pending_worker_ids": []}
        if not changed and pending:
            if source_changed:
                roster["worker_label_profile_mxid"] = profile_mxid
                roster.pop("worker_label_config_version", None)
                _write_atomic(roster_path(workspace), roster)
            _ensure_advertisement(workspace, roster)
            return {"changed": False, "stale": False, "roster_version": roster["version"],
                    "worker_label_config_version": previous, "pending_worker_ids": pending}
        if not changed:
            roster["worker_label_profile_mxid"] = profile_mxid
            roster["worker_label_config_version"] = config_version
            _write_atomic(roster_path(workspace), roster)
            _ensure_advertisement(workspace, roster)
            return {"changed": False, "stale": False, "roster_version": roster["version"],
                    "worker_label_config_version": config_version, "pending_worker_ids": []}
        updated = compile_roster(workspace, workers, load_bindings(workspace),
                                 worker_label_config_version=config_version if not pending else None,
                                 worker_label_profile_mxid=profile_mxid)
        return {"changed": True, "stale": False, "roster_version": updated["version"],
                "worker_label_config_version": config_version if not pending else previous,
                "pending_worker_ids": pending}


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

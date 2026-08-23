"""GitHub adapter for the shepherd contract: turn a real pull request into
observed events, and persist the waiting contract so a later pass can resume.

The contract stays provider-neutral; everything provider-specific is here --
including how actor is resolved. For GitHub that is the commit-author email of
the last non-merge commit, NOT the account login, because several actors can
push under one login and the login cannot discriminate between them.

State lives under the resolved workspace, never under a home-directory path:
the workspace is the durable per-user location and survives app updates.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from local_task_protocol import valid_task_id
from shepherd_contract import (
    SHEPHERD_STATES,
    Actor,
    ObservedEvent,
    ResponsibilityScope,
    Subject,
    admit,
    is_terminal,
    proposed_terminal_state,
    terminal_state_for,
)
from workspace_default import resolve_workspace

PROVIDER = "github"
ACTOR_SCHEME = "git.commit_author_email"

WATCH = frozenset({
    "github.check_suite.completed",
    "github.pull_request.review_submitted",
    "github.pull_request.updated",
})
SUCCESS = frozenset({"github.pull_request.merged"})
FAILURE = frozenset({"github.pull_request.closed_unmerged"})


def state_dir() -> Path:
    return Path(resolve_workspace()) / "state" / "shepherd"


def _gh(*args: str) -> str:
    out = subprocess.run(("gh",) + args, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {out.stderr.strip()[:200]}")
    return out.stdout.strip()


def subject_for(repo: str, number: int) -> Subject:
    return Subject(PROVIDER, "pull_request", f"{repo}#{number}")


def resolve_actor(repo: str, number: int) -> Optional[Actor]:
    """Last non-merge commit's author email. Merge commits carry no authored
    content, so they must not decide whose work a branch is."""
    raw = _gh("api", f"repos/{repo}/pulls/{number}/commits", "--paginate", "-q",
              ".[]|select((.parents|length)<2)|.commit.author.email")
    emails = [e for e in raw.splitlines() if e.strip()]
    return Actor(ACTOR_SCHEME, emails[-1]) if emails else None


def observe(repo: str, number: int) -> ObservedEvent:
    """Current PR state as one event. Terminal states win over progress."""
    raw = _gh("api", f"repos/{repo}/pulls/{number}", "-q",
              "[.state, (.merged|tostring)]|join(\" \")")
    state, merged = (raw.split() + ["", ""])[:2]
    if merged == "true":
        etype = "github.pull_request.merged"
    elif state == "closed":
        etype = "github.pull_request.closed_unmerged"
    else:
        etype = "github.pull_request.updated"
    return ObservedEvent(etype, subject_for(repo, number),
                         resolve_actor(repo, number), source_id=f"{repo}#{number}@{etype}")


def scope_for(repo: str, number: int, actor: Actor) -> ResponsibilityScope:
    return ResponsibilityScope(
        subjects=(subject_for(repo, number),), actor=actor,
        watch_conditions=WATCH, success_conditions=SUCCESS, failure_conditions=FAILURE)


def _contract_path(task_id: str) -> Path:
    """The only place a task_id becomes a path. Unvalidated ids escape the
    directory (`../../x`), and the repository already owns the gate."""
    if not valid_task_id(task_id):
        raise ValueError(f"refusing to build a path from invalid task_id: {task_id!r}")
    return state_dir() / f"{task_id}.json"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-record temp name would be shared by two concurrent writers, and the
    # loser's os.replace() then fails on a file the winner already moved.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _record_lock(task_id: str):
    """Serialize the read-modify-write of one record. Held across the rewrite
    only -- never across a network call."""
    p = _contract_path(task_id).with_suffix(".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _subject_parts(scope: ResponsibilityScope) -> tuple[str, int]:
    """The persisted subject is DERIVED from the scope. Carrying repo/number as
    separate arguments gives one fact two sources, and the record can then be
    reloaded as a different pull request than the one it was accepted for."""
    subs = [s for s in scope.subjects
            if s.provider == PROVIDER and s.kind == "pull_request"]
    if len(subs) != 1:
        raise ValueError(f"expected exactly 1 {PROVIDER} pull_request subject, got {len(subs)}")
    repo, sep, number = subs[0].resource_id.rpartition("#")
    if not sep or not repo.strip() or not number.isdigit():
        raise ValueError(f"malformed subject resource_id: {subs[0].resource_id!r}")
    return repo, int(number)


_REQUIRED_KEYS = ("task_id", "provider", "repo", "number", "actor_scheme",
                  "actor_value", "state", "waiting_for", "success_conditions",
                  "failure_conditions")


def _identity(rec: dict) -> tuple:
    """Everything a stale write could clobber. `note` is free text whose change
    is benign, so it is excluded; every other field is identity-bearing."""
    return (rec["repo"], rec["number"], rec["actor_scheme"], rec["actor_value"],
            tuple(rec["waiting_for"]), tuple(rec["success_conditions"]),
            tuple(rec["failure_conditions"]), rec["state"])


_CONDITION_KEYS = ("waiting_for", "success_conditions", "failure_conditions")
_IDENTITY_KEYS = ("repo", "actor_scheme", "actor_value")


def _validate_record(rec: dict, task_id: str) -> dict:
    """Fail closed on a record that cannot be a real contract. An unvalidated
    load lets a hand-edited or truncated file drive a live objective."""
    if not isinstance(rec, dict):
        raise ValueError(f"contract for {task_id} is not an object")
    missing = [k for k in _REQUIRED_KEYS if k not in rec]
    if missing:
        raise ValueError(f"contract for {task_id} is missing {missing}")
    # The embedded id is the record's own claim about which task it belongs to;
    # only comparing it to the requested id detects a copied or renamed file.
    if rec["task_id"] != task_id:
        raise ValueError(f"contract for {task_id} carries task_id {rec['task_id']!r}")
    if rec["state"] not in SHEPHERD_STATES:
        raise ValueError(f"contract for {task_id} carries invalid state {rec['state']!r}")
    if rec["provider"] != PROVIDER:
        raise ValueError(f"contract for {task_id} is not a {PROVIDER} record")
    if isinstance(rec["number"], bool) or not isinstance(rec["number"], int):
        raise ValueError(f"contract for {task_id} has non-integer number")
    for key in _IDENTITY_KEYS:
        if not isinstance(rec[key], str) or not rec[key].strip():
            raise ValueError(f"contract for {task_id} has blank/non-string {key}")
    at = f"contract for {task_id}"
    for key in _CONDITION_KEYS:
        # A persisted record is JSON, so only a list is a legal container here;
        # element and arity rules are shared with the public seam via _conditions.
        if isinstance(rec[key], str) or not isinstance(rec[key], (list, tuple)):
            raise ValueError(f"{at}: {key} must be a list, "
                             f"got {type(rec[key]).__name__}")
        _conditions(rec, key, required=(key == "waiting_for"), where=at)
    if not (rec["success_conditions"] or rec["failure_conditions"]):
        raise ValueError(f"contract for {task_id}: no reachable outcome — success and "
                         f"failure conditions are both empty")
    if set(rec["success_conditions"]) & set(rec["failure_conditions"]):
        raise ValueError(f"contract for {task_id}: success/failure conditions overlap")
    return rec


def _write_record(task_id: str, scope: ResponsibilityScope, state: str,
                  note: str = "") -> Path:
    """Caller holds the record lock."""
    if state not in SHEPHERD_STATES:
        raise ValueError(f"refusing to persist state {state!r}; not in SHEPHERD_STATES")
    repo, number = _subject_parts(scope)
    p = _contract_path(task_id)
    _atomic_write(p, {
        "task_id": task_id, "provider": PROVIDER, "repo": repo, "number": number,
        "actor_scheme": scope.actor.scheme, "actor_value": scope.actor.value,
        "state": state, "note": note,
        "waiting_for": sorted(scope.watch_conditions),
        "success_conditions": sorted(scope.success_conditions),
        "failure_conditions": sorted(scope.failure_conditions),
    })
    return p


def save(task_id: str, scope: ResponsibilityScope, state: str,
         note: str = "") -> Path:
    """Persist the waiting contract. Atomic: a reader never sees a half-write."""
    with _record_lock(task_id):
        return _write_record(task_id, scope, state, note)


def load(task_id: str) -> Optional[dict]:
    p = _contract_path(task_id)
    if not p.is_file():
        return None
    return _validate_record(json.loads(p.read_text()), task_id)


def _conditions(rec: dict, key: str, *, required: bool = False,
                where: str = "") -> frozenset:
    """One place defines what a condition collection is. A bare str is iterable,
    so frozenset() would silently reconstruct it as a set of characters."""
    at = f"{where}: " if where else ""
    value = rec[key]
    if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(f"{at}{key} must be a collection of strings, "
                         f"got {type(value).__name__}")
    if any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{at}{key} must hold non-empty strings")
    if required and not value:
        raise ValueError(f"{at}{key} must be non-empty")
    return frozenset(value)


def scope_from_saved(rec: dict) -> ResponsibilityScope:
    # This is a public entry point in its own right — load() is not the only way
    # in — so it repeats the arity rule rather than trusting a prior validation.
    success = _conditions(rec, "success_conditions")
    failure = _conditions(rec, "failure_conditions")
    if not (success or failure):
        raise ValueError("no reachable outcome — success and failure conditions "
                         "are both empty")
    return ResponsibilityScope(
        subjects=(subject_for(rec["repo"], rec["number"]),),
        actor=Actor(rec["actor_scheme"], rec["actor_value"]),
        watch_conditions=_conditions(rec, "waiting_for", required=True),
        success_conditions=success,
        failure_conditions=failure)


def resume(task_id: str) -> tuple[str, str]:
    """Re-observe a persisted objective. Returns (state, reason).

    This is the whole point of persistence: it runs in a different process from
    the one that created the contract and needs nothing from that process.
    """
    rec = load(task_id)
    if rec is None:
        return "unknown", f"no persisted contract for {task_id}"
    # A terminal record is final: re-observing must never reopen it, and the
    # network call is pointless once the objective is closed.
    if is_terminal(rec["state"]):
        return rec["state"], f"already terminal ({rec['state']}); not re-observed"

    scope = scope_from_saved(rec)
    snapshot = _identity(rec)
    event = observe(rec["repo"], rec["number"])  # network: NO lock held

    # Another pass may have terminated the objective while we were on the wire;
    # persisting the pre-observation state would silently reopen it.
    with _record_lock(task_id):
        cur = load(task_id)
        if cur is None:
            return "unknown", f"contract for {task_id} disappeared mid-observation"
        prior = cur["state"]
        if is_terminal(prior):
            return prior, f"terminated concurrently ({prior}); observation discarded"
        # The state guard alone is not enough: a concurrent pass can rebind the
        # subject, actor or conditions, and writing back `scope` would revert it.
        if _identity(cur) != snapshot:
            return prior, (f"contract changed during observation "
                           f"(now {cur['repo']}#{cur['number']}, state {prior}); "
                           f"observation discarded")

        decision, why = admit(event, scope)
        if decision != "accepted":
            _write_record(task_id, scope, prior, why)
            return prior, f"{event.event_type} {decision}: {why}"

        terminal = terminal_state_for(event, scope)
        if terminal:
            _write_record(task_id, scope, terminal, why)
            return terminal, f"{event.event_type} accepted -> {terminal}"

        # Progress is not a transition: keep whatever state the objective was in
        # (blocked / needs_human / waiting) rather than flattening it to waiting.
        proposed = proposed_terminal_state(event, scope)
        note = (f"{event.event_type} accepted; outcome proposed={proposed} but actor "
                f"scheme is asserted, not verified — not terminating") if proposed else why
        _write_record(task_id, scope, prior, note)
        return prior, note

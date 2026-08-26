"""GitHub adapter for the shepherd contract: turn a real pull request into
observed events, and persist the waiting contract so a later pass can resume.

The contract (src/shepherd_contract.py) stays provider-neutral; everything
provider-specific lives here, at the optional skill edge -- including how actor
is resolved. For GitHub that is the commit-author email of the last non-merge
commit, NOT the account login, because several actors can push under one login
and the login cannot discriminate between them.

State lives under the resolved workspace, never under a home-directory path:
the workspace is the durable per-user location and survives app updates.
"""

from __future__ import annotations

import re
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from local_task_protocol import valid_task_id  # noqa: E402
from shepherd_contract import (  # noqa: E402
    Actor,
    ObservedEvent,
    ResponsibilityScope,
    Subject,
    admit,
    is_terminal,
    proposed_terminal_state,
    register_actor_scheme,
    require_shepherd_state,
    terminal_state_for,
)
from workspace_default import resolve_workspace  # noqa: E402

PROVIDER = "github"
ACTOR_SCHEME = "git.commit_author_email"
# Only the scheme GitHub resolves; a verified scheme belongs to whatever
# seam authenticates it (a Matrix adapter or a test fixture), never here.
register_actor_scheme(ACTOR_SCHEME)

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


PR_STATES = frozenset({"open", "closed"})
MERGED_TOKENS = frozenset({"true", "false"})
VALID_PR_PROJECTIONS = frozenset({("open", "false"), ("closed", "false"), ("closed", "true")})


_ASCII_DIGITS = frozenset("0123456789")


# Owner logins never lead with a dot; repo NAMES may (`.github`). \Z not $:
# under .match(), $ accepts a terminal newline and persists "owner/repo\n".
_OWNER_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_NAME_SEGMENT = re.compile(r"^(?!\.\.?$)[A-Za-z0-9._-]+\Z")


def _pr_repo(value: object, where: str) -> str:
    """Canonical `owner/name` only: the value is interpolated into a gh API
    path, so anything looser (extra segments, `..`, whitespace, query text)
    addresses a different resource than the subject it is stored under."""
    if type(value) is not str:
        raise ValueError(f"{where}: repo must be a str (exact type), got "
                         f"{type(value).__name__}")
    parts = value.split("/")
    if (len(parts) != 2 or not _OWNER_SEGMENT.match(parts[0])
            or not _NAME_SEGMENT.match(parts[1])):
        raise ValueError(f"{where}: repo must be canonical owner/name "
                         f"(exactly two path-safe segments), got {value!r}")
    return value


def _pr_number(value: object, where: str) -> int:
    """The ONE place a PR number is judged. `str.isdigit()` is true for Unicode
    digits and for leading zeros, and int() then canonicalizes them -- so the
    subject that loads is not the subject that was written."""
    # EXACT types: an int SUBCLASS returned unchanged can override comparison
    # and formatting, defeating the positivity and canonicalization rules below.
    if type(value) not in (int, str):
        raise ValueError(f"{where}: PR number must be an int or digit str "
                         f"(exact type), got {type(value).__name__}")
    if type(value) is str:
        if not value or not set(value) <= _ASCII_DIGITS:
            raise ValueError(f"{where}: PR number {value!r} is not ASCII digits")
        if value != str(int(value)):
            raise ValueError(f"{where}: PR number {value!r} is not canonical")
        value = int(value)
    if value <= 0:
        raise ValueError(f"{where}: PR number must be positive, got {value}")
    return value


def subject_for(repo: str, number: int) -> Subject:
    """Construction seam: every GitHub subject in the process is built here, so
    validating here covers scope_for, observe and public rehydration alike."""
    return Subject(PROVIDER, "pull_request",
                   f"{_pr_repo(repo, 'subject_for')}#{_pr_number(number, 'subject_for')}")


def resolve_actor(repo: str, number: int) -> Optional[Actor]:
    """Last non-merge commit's author email. Merge commits carry no authored
    content, so they must not decide whose work a branch is."""
    repo = _pr_repo(repo, "resolve_actor")
    number = _pr_number(number, "resolve_actor")
    raw = _gh("api", f"repos/{repo}/pulls/{number}/commits", "--paginate", "-q",
              ".[]|select((.parents|length)<2)|.commit.author.email")
    emails = [e for e in raw.splitlines() if e.strip()]
    return Actor(ACTOR_SCHEME, emails[-1]) if emails else None


def observe(repo: str, number: int) -> ObservedEvent:
    """Current PR state as one event. Terminal states win over progress."""
    repo = _pr_repo(repo, "observe")
    number = _pr_number(number, "observe")
    raw = _gh("api", f"repos/{repo}/pulls/{number}", "-q",
              "[.state, (.merged|tostring)]|join(\" \")")
    # An unrecognized projection is UNKNOWN, not "not merged": padding it out
    # would publish a concrete failure proposal for a state we never observed.
    parts = raw.split()
    if len(parts) != 2:
        raise ValueError(f"unreadable PR projection for {repo}#{number}: {raw!r}")
    state, merged = parts
    if state not in PR_STATES or merged not in MERGED_TOKENS:
        raise ValueError(
            f"unknown PR projection for {repo}#{number}: state={state!r} merged={merged!r}")
    # Both fields come from ONE API object, so an impossible PAIR is malformed
    # evidence, not a terminal state that should win. A merged PR is never open.
    if (state, merged) not in VALID_PR_PROJECTIONS:
        raise ValueError(
            f"impossible PR projection for {repo}#{number}: "
            f"state={state!r} merged={merged!r}")
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
    directory (`../../x`), and the repository already owns the gate.

    EXACT str first: a subclass can satisfy valid_task_id() on its underlying
    value while __format__ renders something else into the path."""
    if type(task_id) is not str:
        raise ValueError(f"task_id must be an exact str, got {type(task_id).__name__}")
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
    # Unlocking a lock never acquired is wrong, and one flat finally lets a failed
    # unlock skip the close -- leaking the descriptor with the lock still held.
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _subject_parts(scope: ResponsibilityScope) -> tuple[str, int]:
    """The persisted subject is DERIVED from the scope. Carrying repo/number as
    separate arguments gives one fact two sources, and the record can then be
    reloaded as a different pull request than the one it was accepted for."""
    subs = [s for s in scope.subjects
            if s.provider == PROVIDER and s.kind == "pull_request"]
    if len(subs) != 1:
        raise ValueError(f"expected exactly 1 {PROVIDER} pull_request subject, got {len(subs)}")
    # The record encodes ONE subject, so a wider scope would be silently narrowed
    # on reload -- resuming a different contract than the one accepted.
    extra = [s for s in scope.subjects if s not in subs]
    if extra:
        raise ValueError(
            f"scope carries {len(extra)} subject(s) this record cannot encode: "
            f"{[f'{s.provider}:{s.kind}:{s.resource_id}' for s in extra]}")
    repo, sep, number = subs[0].resource_id.rpartition("#")
    at = f"malformed subject resource_id {subs[0].resource_id!r}"
    if not sep:
        raise ValueError(at)
    return _pr_repo(repo, at), _pr_number(number, at)


_REQUIRED_KEYS = ("task_id", "provider", "repo", "number", "actor_scheme",
                  "actor_value", "state", "waiting_for", "success_conditions",
                  "failure_conditions")


def _binding(rec: dict) -> tuple:
    """WHICH objective this record is -- subject, actor, conditions. Excludes
    state, the one field a legal update is allowed to move."""
    return (rec["repo"], rec["number"], rec["actor_scheme"], rec["actor_value"],
            tuple(rec["waiting_for"]), tuple(rec["success_conditions"]),
            tuple(rec["failure_conditions"]))


def _identity(rec: dict) -> tuple:
    """Everything a stale write could clobber. `note` is free text whose change
    is benign, so it is excluded; every other field is identity-bearing."""
    return _binding(rec) + (rec["state"],)


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
    try:
        require_shepherd_state(rec["state"], f"contract for {task_id}")
    except ValueError as exc:
        raise ValueError(f"contract for {task_id} carries invalid state "
                         f"{rec['state']!r}") from exc
    if rec["provider"] != PROVIDER:
        raise ValueError(f"contract for {task_id} is not a {PROVIDER} record")
    at = f"contract for {task_id}"
    # JSON round-trips an int, so a string here is a hand-edit or a foreign
    # writer -- the persisted schema is stricter than the construction seam.
    if isinstance(rec["number"], bool) or not isinstance(rec["number"], int):
        raise ValueError(f"{at} has non-integer number")
    _pr_number(rec["number"], at)
    _pr_repo(rec["repo"], at)
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


def _record_payload(task_id: str, scope: ResponsibilityScope, state: str,
                    note: str = "") -> dict:
    """The record a write WOULD produce. Separated from the write so save() can
    compare the proposed binding against the stored one before committing."""
    # EXACT type: a subclass can answer the binding check from one value and a
    # later attribute read from another, so it must never reach a payload build.
    if type(scope) is not ResponsibilityScope:
        raise TypeError(f"scope must be exactly ResponsibilityScope, "
                        f"got {type(scope).__name__}")
    state = require_shepherd_state(state, "refusing to persist")
    repo, number = _subject_parts(scope)
    payload = {
        "task_id": task_id, "provider": PROVIDER, "repo": repo, "number": number,
        "actor_scheme": scope.actor.scheme, "actor_value": scope.actor.value,
        "state": state, "note": note,
        "waiting_for": sorted(scope.watch_conditions),
        "success_conditions": sorted(scope.success_conditions),
        "failure_conditions": sorted(scope.failure_conditions),
    }
    # The loader's schema, applied before publish -- not a copy of it. A record
    # that save() accepts but load() rejects is a contract no successor can resume.
    return _validate_record(payload, task_id)


def _write_record(task_id: str, payload: dict) -> Path:
    """Caller holds the record lock, established the transition is legal, and
    built `payload` via _record_payload: the dict that passed the caller's
    checks is the dict written -- never rebuilt from the (overridable) scope."""
    p = _contract_path(task_id)
    _atomic_write(p, payload)
    return p


def save(task_id: str, scope: ResponsibilityScope, state: str,
         note: str = "") -> Path:
    """Persist the waiting contract. Atomic: a reader never sees a half-write.

    Create-or-advance, never rebind: an existing record may only move its STATE.
    Without this, two creators racing on one task id are last-writer-wins, and a
    finished objective can be reopened against a different pull request.
    """
    # Built ONCE, before the lock: invalid input never takes the lock, and the
    # payload compared below is byte-for-byte the payload committed.
    proposed = _record_payload(task_id, scope, state, note)
    with _record_lock(task_id):
        prior = load(task_id)
        if prior is not None:
            if _binding(proposed) != _binding(prior):
                raise ValueError(
                    f"contract for {task_id} is bound to {prior['repo']}#{prior['number']} "
                    f"({prior['actor_scheme']}:{prior['actor_value']}); refusing to rebind "
                    f"to {proposed['repo']}#{proposed['number']}")
            # Terminal is final for the public seam: resume() reaches a terminal
            # state through its own guarded write, never through here.
            if is_terminal(prior["state"]) and prior["state"] != state:
                raise ValueError(
                    f"contract for {task_id} is terminal ({prior['state']}); "
                    f"refusing to reopen as {state!r}")
        return _write_record(task_id, proposed)


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
    if rec.get("provider") != PROVIDER:
        raise ValueError(f"not a {PROVIDER} record: provider={rec.get('provider')!r}")
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
            _write_record(task_id, _record_payload(task_id, scope, prior, why))
            return prior, f"{event.event_type} {decision}: {why}"

        terminal = terminal_state_for(event, scope)
        if terminal:
            _write_record(task_id, _record_payload(task_id, scope, terminal, why))
            return terminal, f"{event.event_type} accepted -> {terminal}"

        # Progress is not a transition: keep whatever state the objective was in
        # (blocked / needs_human / waiting) rather than flattening it to waiting.
        proposed = proposed_terminal_state(event, scope)
        note = (f"{event.event_type} accepted; outcome proposed={proposed} but actor "
                f"scheme is asserted, not verified — not terminating") if proposed else why
        _write_record(task_id, _record_payload(task_id, scope, prior, note))
        return prior, note

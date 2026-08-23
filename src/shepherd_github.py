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

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from shepherd_contract import (
    Actor,
    ObservedEvent,
    ResponsibilityScope,
    Subject,
    admit,
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


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    os.replace(tmp, path)


def save(task_id: str, repo: str, number: int, scope: ResponsibilityScope,
         state: str, note: str = "") -> Path:
    """Persist the waiting contract. Atomic: a reader never sees a half-write."""
    p = state_dir() / f"{task_id}.json"
    _atomic_write(p, {
        "task_id": task_id, "provider": PROVIDER, "repo": repo, "number": number,
        "actor_scheme": scope.actor.scheme, "actor_value": scope.actor.value,
        "state": state, "note": note,
        "waiting_for": sorted(scope.watch_conditions),
        "success_conditions": sorted(scope.success_conditions),
        "failure_conditions": sorted(scope.failure_conditions),
    })
    return p


def load(task_id: str) -> Optional[dict]:
    p = state_dir() / f"{task_id}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def scope_from_saved(rec: dict) -> ResponsibilityScope:
    return ResponsibilityScope(
        subjects=(subject_for(rec["repo"], rec["number"]),),
        actor=Actor(rec["actor_scheme"], rec["actor_value"]),
        watch_conditions=frozenset(rec["waiting_for"]),
        success_conditions=frozenset(rec["success_conditions"]),
        failure_conditions=frozenset(rec["failure_conditions"]))


def resume(task_id: str) -> tuple[str, str]:
    """Re-observe a persisted objective. Returns (state, reason).

    This is the whole point of persistence: it runs in a different process from
    the one that created the contract and needs nothing from that process.
    """
    rec = load(task_id)
    if rec is None:
        return "unknown", f"no persisted contract for {task_id}"
    scope = scope_from_saved(rec)
    event = observe(rec["repo"], rec["number"])
    decision, why = admit(event, scope)
    if decision != "accepted":
        save(task_id, rec["repo"], rec["number"], scope, rec["state"], why)
        return rec["state"], f"{event.event_type} {decision}: {why}"
    terminal = terminal_state_for(event, scope)
    new_state = terminal or "waiting"
    save(task_id, rec["repo"], rec["number"], scope, new_state, why)
    return new_state, f"{event.event_type} accepted"

"""Claude readiness detector — the Requirement Detector half of the runtime
supervisor, and the one-state ClaudeTuiDriver v0 (AUTH_REQUIRED only).

Readiness is probed actively (`claude auth status --json`), never inferred
from TUI screen text: the probe works before any session exists and survives
TUI redesigns. The probe has THREE verdicts — ready, not-ready, and unknown
(probe failed) — and unknown creates nothing: a broken probe must not spam
attention cards.

drive() is the whole driver loop body: not-ready creates/refreshes the auth
requirement (Manager dedups per (runtime, kind)); ready resolves it and
returns the blocked task ids to resume. Resolution never requires an action
click — the user may fix auth out-of-band and the next probe clears it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .manager import HitlManager
from .schema import Action, HumanRequirement

RUNTIME = "claude"
PROBE_CMD = ["claude", "auth", "status", "--json"]

# runner(cmd) -> (rc, stdout) — injected so tests never spawn the CLI.
Runner = Callable[[List[str]], "tuple[int, str]"]


def _default_runner(cmd: List[str]) -> "tuple[int, str]":
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return proc.returncode, proc.stdout


@dataclass
class ProbeResult:
    # True = authenticated, False = sign-in needed, None = probe could not answer.
    ready: Optional[bool]
    detail: Dict = field(default_factory=dict)
    guard: str = ""


def probe_claude_auth(runner: Runner = _default_runner) -> ProbeResult:
    try:
        rc, out = runner(PROBE_CMD)
    except (OSError, subprocess.TimeoutExpired):
        return ProbeResult(ready=None)
    try:
        detail = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return ProbeResult(ready=None)
    if not isinstance(detail, dict) or "loggedIn" not in detail:
        return ProbeResult(ready=None)
    logged_in = bool(detail.get("loggedIn"))
    # Guard = digest of the auth-relevant fields: a changed auth state beneath
    # a pending card bumps the guard, staling any action against the old one.
    basis = json.dumps(
        {k: detail.get(k) for k in ("loggedIn", "authMethod", "email", "orgId")},
        sort_keys=True,
    )
    guard = "auth:" + hashlib.sha1(basis.encode()).hexdigest()[:16]
    return ProbeResult(ready=logged_in, detail=detail, guard=guard)


def auth_requirement(guard: str, device: Optional[Dict[str, str]] = None) -> HumanRequirement:
    return HumanRequirement(
        kind="auth",
        runtime=RUNTIME,
        message="Claude Code needs to sign in again",
        guard=guard,
        device=device,
        actions=[Action(id="reauth", kind="authenticate", label="Re-authenticate")],
    )


@dataclass
class DriveOutcome:
    created: Optional[str] = None  # requirement id created or refreshed
    resolved: List[str] = field(default_factory=list)  # requirement ids resolved
    resumed_tasks: List[str] = field(default_factory=list)


def drive(
    manager: HitlManager,
    device: Optional[Dict[str, str]] = None,
    runner: Runner = _default_runner,
) -> DriveOutcome:
    """One detector pass: probe, then converge requirement state to reality."""
    outcome = DriveOutcome()
    result = probe_claude_auth(runner)
    if result.ready is None:
        return outcome
    active_auth = [
        r for r in manager.active() if r.runtime == RUNTIME and r.kind == "auth"
    ]
    if result.ready:
        for req in active_auth:
            outcome.resumed_tasks.extend(manager.resolve(req.id))
            outcome.resolved.append(req.id)
        return outcome
    req = manager.create(auth_requirement(result.guard, device))
    outcome.created = req.id
    return outcome

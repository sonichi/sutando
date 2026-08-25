#!/usr/bin/env python3
"""Continuity-first startup policy for a pool core.

The rule this encodes, in one line: **resume whenever technically possible;
roll over only on evidence that continuation is unavailable or unhealthy.**

Two consequences follow, and both are deliberate inversions of the obvious
design:

Size and age never gate a resume. A large transcript makes resuming
expensive, and expensive is not a reason to silently discard a
conversation. They produce advisories — compact after resuming, consider a
rollover — which never change the action.

A burst of rapid deaths is not proof the session is at fault. A broken
config kills a fresh session just as fast. So attribution is diagnosed, not
counted: retry the same session to confirm the failure reproduces, then
start an isolated probe that differs by exactly one variable — the resume
target — and blame the session only if the probe is healthy. If the probe
dies too, the fault is environmental and the answer is to back off, not to
manufacture more sessions.

Pure policy: no filesystem, no subprocess, no clock. The caller supplies the
head generation, the attempt history and the probe outcome, and receives a
decision. Injected everything; stdlib only.
"""
from __future__ import annotations

# preassign: the caller may choose the id before the session exists
# (claude --session-id). resume: an existing id can be continued.
RUNTIME_RESUME_CAPABILITY = {
    "claude": {"resume": True, "preassign": True},
    "codex": {"resume": True, "preassign": False},
}

# Failures on the SAME session before its failure counts as reproducible.
# One is a coincidence; the retry is what makes the probe worth paying for.
CONFIRM_ATTEMPTS = 2

RESUME = "resume"
NEW = "new"
PROBE = "probe"
BACKOFF = "backoff"


def runtime_capability(runtime: str) -> dict:
    """An unknown runtime gets the least-capable answer, not an exception —
    a startup path must still start something."""
    return RUNTIME_RESUME_CAPABILITY.get(
        runtime, {"resume": False, "preassign": False})


def failures_on(session_id: "str | None", attempts) -> int:
    """Consecutive trailing failures against this exact session. A success
    anywhere resets it: the session demonstrably still loads."""
    if not session_id:
        return 0
    n = 0
    for a in reversed(list(attempts or [])):
        if a.get("session_id") != session_id:
            continue
        if a.get("ok"):
            break
        n += 1
    return n


def decide(head, attempts=(), probe_ok=None) -> dict:
    """Return the startup action for a seated profile.

    head: the profile's head generation (or None when it has none yet)
    attempts: prior startup attempts, oldest first, each {session_id, ok}
    probe_ok: outcome of an isolated fresh-session probe, once one has run
    """
    session = (head or {}).get("session_id")
    runtime = (head or {}).get("runtime", "claude")
    if not session:
        return {"action": NEW, "reason": "initial", "session_id": None,
                "note": "no recorded session; starting a first generation"}
    if not runtime_capability(runtime)["resume"]:
        return {"action": NEW, "reason": "runtime_switch", "session_id": None,
                "note": f"{runtime} cannot resume by id; starting fresh — "
                        f"this is a continuity break, not a resume"}

    failed = failures_on(session, attempts)
    if failed < CONFIRM_ATTEMPTS:
        return {"action": RESUME, "session_id": session, "reason": None,
                "note": ("resuming" if failed == 0 else
                         "retrying the same session to confirm the failure "
                         "reproduces before blaming it")}
    if probe_ok is None:
        return {"action": PROBE, "session_id": None, "reason": None,
                "note": ("failure reproduced; starting an isolated probe that "
                         "differs only in not resuming, to find out whether "
                         "the session or the environment is at fault")}
    if probe_ok:
        return {"action": NEW, "reason": "resume_failed",
                "session_id": None, "quarantine": session,
                "note": (f"probe healthy without --resume, so {session} is "
                         f"the fault; quarantining it and breaking continuity")}
    return {"action": BACKOFF, "session_id": None, "reason": None,
            "note": ("a fresh session fails the same way, so the fault is "
                     "environmental — backing off instead of creating more "
                     "sessions that will die identically")}


def advisories(head, transcript_bytes=None, age_s=None, policy=None) -> list:
    """Size/age observations that NEVER change the action.

    Returned so a caller can compact after resuming or suggest a rollover;
    a caller that uses these to skip a resume has broken continuity-first.
    """
    policy = policy or {}
    out = []
    max_bytes = policy.get("max_bytes")
    max_age = policy.get("max_age_s")
    if (max_bytes and transcript_bytes is not None
            and transcript_bytes > max_bytes):
        out.append("compact_after_resume")
    if max_age and age_s is not None and age_s > max_age:
        out.append("rollover_suggested")
    return out

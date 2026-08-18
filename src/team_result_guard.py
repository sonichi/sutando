#!/usr/bin/env python3
"""Final scan applied to a Team-tier result before any router reads its markers.

A Team result is produced under a prompt its sender can influence, so the last
thing between that text and a marker-interpreting router must be code, not the
producer's compliance with an instruction.

Ownership: this module is the single implementation of the policy — the control
marker set, the secret scan, and what a withheld result says. Adapters bind
their own tier lookup and delivery; they must not restate any of it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# LEGACY re-export only — detection now derives from result_markers.parse_markers
# (per-family slot rules); this unanchored form over-matched prose mentions.
TEAM_RESULT_CONTROL = re.compile(
    r"\[(?:channel|file|send|attach|dm-only|no-send|replied|deduped)\s*(?::|\])",
    re.IGNORECASE,
)

TEAM_LEAK_RESULT = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information or delivery-control markers. The original "
    "output is saved for owner review under state/withheld-team-results/ on "
    "the host."
)

# The reply when persistence ITSELF failed: never claim a saved copy that
# does not exist, and never release the body either.
TEAM_LEAK_RESULT_UNSAVED = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information or delivery-control markers. Saving the "
    "original for owner review ALSO failed, so no copy exists."
)

# Withheld bodies are persisted here (workspace-relative) so the placeholder
# above is TRUE — before this existed the body was simply dropped.
WITHHELD_DIR_RELPATH = "state/withheld-team-results"

# Only `owner` is exempt — markers are a feature for the owner and a capability
# for everyone else. Stated as an exemption so an unknown tier guards by default.
OWNER_TIER = "owner"


def is_guarded_tier(tier) -> bool:
    """Every tier except `owner` is guarded, including empty and unrecognised."""
    return (tier or "").strip().lower() != OWNER_TIER


class TeamResultLeakError(RuntimeError):
    """A Team result carried a delivery-control marker or a likely secret."""


def resolve_access_tier(task_file) -> str:
    """Read a task's effective tier without letting a task-last body escalate.

    Task-last writers put the trusted tier before ``task:``; prefer that value.
    The remote gateway is task-mid and newline-confines every wire value, so if
    no pre-task tier exists its final tier line is the trusted value.  Missing
    legacy tiers remain owner; malformed explicit tiers fail closed to guest.
    """
    try:
        content = Path(task_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "guest"
    before_task = content.split("\ntask:", 1)[0]
    candidates = [
        line.partition(":")[2].strip().lower()
        for line in before_task.splitlines()
        if line.startswith("access_tier:")
    ]
    if not candidates:
        candidates = [
            line.partition(":")[2].strip().lower()
            for line in content.splitlines()
            if line.startswith("access_tier:")
        ]
    if not candidates:
        return "owner"
    tier = candidates[-1]
    if tier == "other":
        tier = "guest"
    return tier if tier in {"owner", "team", "guest"} else "guest"


def load_team_result_scanner(repo: Path):
    """Load and warm the full scanner graph before Team-controlled execution."""
    source_dir = str((Path(repo) / "src").resolve())
    if source_dir not in sys.path:
        sys.path.insert(0, source_dir)
    try:
        # Warm the full scanner graph before Team runs; later source rewrites
        # cannot replace the retained parent-process module objects.
        from chat_secret_filter import filter_chat_secrets
        try:
            from secret_scanner import scan_and_redact as retained_scan_and_redact
        except Exception:
            # Optional detector dependency; the curated fallback stays valid.
            retained_scan_and_redact = None
        warmup = filter_chat_secrets("Sutando Team result scanner warmup")
        if not hasattr(warmup, "detected") or (
            retained_scan_and_redact is not None
            and not callable(retained_scan_and_redact)
        ):
            raise TypeError("invalid Team result scanner contract")
    except Exception as exc:
        raise RuntimeError("Team result secret scanner is unavailable") from exc
    return filter_chat_secrets


def _control_marker_families(body: str) -> "list[str]":
    """Marker families a CONSUMER would act on, from the canonical grammar.

    src/result_markers.py owns per-family slot rules (skip/redirect anchored,
    attach unanchored, dm-only anywhere). Deriving detection from the same
    parser keeps the guard exactly as wide as every consumer, per family —
    a mid-prose MENTION of an anchored marker is prose, not an action."""
    from result_markers import parse_markers
    seen: list[str] = []
    for action in parse_markers(body).actions:
        if action.kind not in seen:
            seen.append(action.kind)
    return seen


def scan_team_result(body: str, repo: Path, secret_filter=None) -> str:
    """Return `body` unchanged, or raise TeamResultLeakError if it must be withheld."""
    try:
        families = _control_marker_families(body)
    except Exception as exc:
        raise TeamResultLeakError(f"marker grammar unavailable: {exc}") from exc
    if families:
        raise TeamResultLeakError(
            "delivery-control marker in effect: " + ", ".join(families))
    filter_chat_secrets = secret_filter or load_team_result_scanner(repo)
    try:
        result = filter_chat_secrets(body)
    except Exception as exc:
        raise RuntimeError("Team result secret scan failed") from exc
    if result.detected:
        raise TeamResultLeakError(", ".join(result.secret_types))
    return body


def guard_result_for_tier(body: str, tier, repo: Path, secret_filter=None):
    """Consumer-facing gate: returns (safe_body, withheld_reason).

    Returns a body rather than raising, so a caller cannot deliver the raw text
    by catching an exception — the safe body is the only one it is handed.
    """
    if not is_guarded_tier(tier):
        return body, None
    try:
        return scan_team_result(body, repo, secret_filter), None
    except TeamResultLeakError as exc:
        return _withheld_reply(body, str(exc)), str(exc)
    except Exception as exc:
        # Scanner unavailable is fail-CLOSED: an unscannable guarded result is
        # withheld, never delivered on the assumption that it was probably fine.
        reason = f"scanner unavailable: {exc}"
        return _withheld_reply(body, reason), reason


def _withheld_reply(body: str, reason: str) -> str:
    """The placeholder claims a saved copy only when one actually exists."""
    if persist_withheld_body(body, reason) is not None:
        return TEAM_LEAK_RESULT
    return TEAM_LEAK_RESULT_UNSAVED


def persist_withheld_body(body: str, reason: str) -> "str | None":
    """Save a withheld body for owner review; returns the path or None.

    Best-effort BY DESIGN: a persistence failure must never resurrect the
    body or fail the withhold — the guard's verdict stands either way."""
    try:
        import os
        import time
        from workspace_default import resolve_workspace
        directory = resolve_workspace() / WITHHELD_DIR_RELPATH
        directory.mkdir(parents=True, exist_ok=True)
        # uuid4 suffix: ms+pid alone collides for two withholds in the same
        # process and millisecond; a swallowed O_EXCL failure then falsifies
        # the placeholder's "saved" claim.
        import uuid
        path = directory / (
            f"withheld-{int(time.time() * 1000)}-{os.getpid()}"
            f"-{uuid.uuid4().hex[:8]}.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"withheld_reason: {reason}\n---\n{body}")
        return str(path)
    except Exception:
        return None

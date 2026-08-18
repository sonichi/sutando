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

import sys
from pathlib import Path
from typing import NamedTuple

# A marker is a control only where the shared parser EXECUTES it, so the guard
# derives its classification from parse_markers rather than a parallel grammar.
try:
    from .result_markers import parse_markers  # packaged sibling (ag2-sparrow)
except ImportError:
    from result_markers import parse_markers  # monorepo src/ on sys.path

TEAM_LEAK_RESULT = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information. The owner can review the work locally."
)

TEAM_SUPPRESS_RESULT = (
    "Task handled. The agent marked this reply as not-for-delivery; suppression "
    "markers are not honoured on Team-tier results, so this notice is delivered "
    "in its place. Nothing was withheld for content reasons."
)

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


def scan_team_result(body: str, repo: Path, secret_filter=None) -> str:
    """Return `body` unchanged, or raise TeamResultLeakError if it must be withheld."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    # dm-only only suppresses a redirect the guard already withholds, and a
    # redirect it suppressed never executes -- neither is a control here.
    if kinds & {"redirect", "attach"}:
        raise TeamResultLeakError("result delivery control marker")
    if "skip" in kinds:
        raise TeamResultLeakError("suppressive delivery marker")
    filter_chat_secrets = secret_filter or load_team_result_scanner(repo)
    try:
        result = filter_chat_secrets(body)
    except Exception as exc:
        raise RuntimeError("Team result secret scan failed") from exc
    if result.detected:
        raise TeamResultLeakError(", ".join(result.secret_types))
    return body


VERDICT_DELIVER = "deliver"
VERDICT_LEAK = "leak"
VERDICT_SUPPRESS = "suppress"


class TeamResultVerdict(NamedTuple):
    """kind: deliver | leak | suppress. body: what a notice-mechanics adapter
    delivers; an adapter with a durable transport journal may realise SUPPRESS
    as a journaled silent close instead -- the record requirement is the policy."""
    kind: str
    body: str
    reason: "str | None"


def classify_result_for_tier(body: str, tier, repo: Path,
                             secret_filter=None) -> TeamResultVerdict:
    """The guard-owned policy verdict. Adapters apply transport mechanics only;
    re-deciding (or bypassing) this classification in a bridge is a boundary
    violation, not an implementation choice."""
    if not is_guarded_tier(tier):
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    try:
        return TeamResultVerdict(
            VERDICT_DELIVER, scan_team_result(body, repo, secret_filter), None)
    except TeamResultLeakError as exc:
        if str(exc) == "suppressive delivery marker":
            return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT, str(exc))
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT, str(exc))
    except Exception as exc:
        # Scanner unavailable is fail-CLOSED: an unscannable guarded result is
        # withheld, never delivered on the assumption that it was probably fine.
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT,
                                 f"scanner unavailable: {exc}")


def guard_result_for_tier(body: str, tier, repo: Path, secret_filter=None):
    """Consumer-facing gate: returns (safe_body, withheld_reason).

    Returns a body rather than raising, so a caller cannot deliver the raw text
    by catching an exception -- the safe body is the only one it is handed.
    Derived from classify_result_for_tier so the verdict has exactly one owner.
    """
    verdict = classify_result_for_tier(body, tier, repo, secret_filter)
    return verdict.body, verdict.reason

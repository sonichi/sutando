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

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
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

# Safe to name: the marker is the sender's own construct. Content/secret
# classes stay generic — naming those would confirm probe hits.
TEAM_LEAK_RESULT_MARKER = (
    "I completed the Team task, but the response was withheld because it "
    "carried a delivery-control marker, which non-owner results may not use — "
    "not because of its content. The owner can review the work locally."
)

TEAM_LEAK_RESULT_UNSAVED = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information or delivery-control markers. Preparing the "
    "private owner review failed; the result remains withheld and will retry."
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
    # LF-only split: the writer strips only \r/\n, so a Unicode line
    # boundary in a field must not forge a header. str.splitlines() would.
    def _tiers(text):
        return [
            line.partition(":")[2].strip().lower()
            for line in text.split("\n")
            if line.startswith("access_tier:")
        ]
    before_task = content.split("\ntask:", 1)[0]
    candidates = _tiers(before_task) or _tiers(content)
    if not candidates:
        return "owner"
    # Conflicting explicit tiers can only come from injection — fail closed.
    normed = {"guest" if t == "other" else t for t in candidates}
    if len(normed) > 1:
        return "guest"
    tier = candidates[-1]
    if tier == "other":
        tier = "guest"
    return tier if tier in {"owner", "team", "guest"} else "guest"


def sensitive_data_filter_enabled(task_file, tier=None) -> bool:
    """Disable only for paired Team collaborator and filter-off stamps."""
    if tier != "team":
        return True
    try:
        content = Path(task_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    # LF-only split (see resolve_access_tier): a Unicode line boundary in a
    # field must not forge a filter-off / collaborator stamp.
    before_task = content.split("\ntask:", 1)[0].split("\n")
    filter_values = [
        line.partition(":")[2].strip()
        for line in before_task
        if line.startswith("sensitive_data_filter:")
    ]
    collaborator_values = [
        line.partition(":")[2].strip()
        for line in before_task
        if line.startswith("collaborator:")
    ]
    return filter_values != ["false"] or collaborator_values != ["true"]


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


def scan_team_result(body: str, repo: Path, secret_filter=None,
                     scan_sensitive_data: bool = True) -> str:
    """Return `body` unchanged, or raise TeamResultLeakError if it must be withheld."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    # dm-only only suppresses a redirect the guard already withholds, and a
    # redirect it suppressed never executes -- neither is a control here.
    if kinds & {"redirect", "attach"}:
        raise TeamResultLeakError("result delivery control marker")
    if "skip" in kinds:
        raise TeamResultLeakError("suppressive delivery marker")
    # Marker checks stay above this narrow scanner opt-out.
    if not scan_sensitive_data:
        return body
    filter_chat_secrets = secret_filter or load_team_result_scanner(repo)
    try:
        result = filter_chat_secrets(body)
    except Exception as exc:
        raise RuntimeError("Team result secret scan failed") from exc
    if result.detected:
        raise TeamResultLeakError(", ".join(result.secret_types))
    return body


# The suppression verdict: one policy here, transport mechanics at the
# edges (stub-alone where the server suppresses; notice where it delivers).
_SKIP_STUB_LITERAL = {"no-send": "[no-send]", "REPLIED": "[REPLIED]"}
_DEDUP_EXTRA_RE = re.compile(r"task-[A-Za-z0-9_-]{1,64}\Z")


def suppression_stub_for_tier(body: str, tier) -> "str | None":
    """The stub a guarded sender may close its lease with, or None.

    None means NO suppression verdict -- the caller proceeds through
    guard_result_for_tier as before (owner results, mixed markers,
    out-of-grammar dedup extras, unknown future markers all land here)."""
    if not is_guarded_tier(tier):
        return None
    parsed = parse_markers(body)
    if not parsed.actions or any(a.kind != "skip" for a in parsed.actions):
        return None
    a = parsed.actions[0]
    if a.value == "deduped":
        extra = (a.extra or "").strip()
        if _DEDUP_EXTRA_RE.fullmatch(extra):
            return f"[deduped: {extra}]"
        return None
    return _SKIP_STUB_LITERAL.get(a.value)


VERDICT_DELIVER = "deliver"
VERDICT_LEAK = "leak"
VERDICT_SUPPRESS = "suppress"
WITHHELD_RESULT_DIR = "withheld-team-results"
SUPPRESSED_RESULT_DIR = "suppressed-team-results"


class TeamResultVerdict(NamedTuple):
    """kind: deliver | leak | suppress. body: what a notice-mechanics adapter
    delivers; an adapter with a durable transport journal may realise SUPPRESS
    as a journaled silent close instead -- the record requirement is the policy."""
    kind: str
    body: str
    reason: "str | None"


def _bounded_context(context) -> dict:
    if not isinstance(context, dict):
        return {}
    keys = (
        "source", "channel_id", "room_name", "reply_to_event",
        "source_message_id", "user_id",
    )
    return {key: str(context.get(key) or "")[:512] for key in keys}


def withheld_review_id(task_id: str) -> str:
    identity = task_id.encode() if task_id else os.urandom(32)
    return f"wr_{hashlib.sha256(identity).hexdigest()[:16]}"


def withheld_review_path(state_dir: Path, task_id: str) -> Path:
    return Path(state_dir) / WITHHELD_RESULT_DIR / f"{withheld_review_id(task_id)}.json"


def _write_artifact(path: Path, payload: dict) -> bool:
    if path.is_file():
        return True
    fd, temporary = tempfile.mkstemp(prefix=".withheld-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return path.is_file()
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def materialize_withheld_verdict(verdict: TeamResultVerdict, body: str,
                                 state_dir: Path, task_id: str, context=None,
                                 agent_id: str = "", now=None) -> TeamResultVerdict:
    """Persist a leak verdict for private owner review and suppress the room post."""
    if verdict.kind != VERDICT_LEAK:
        return verdict
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / WITHHELD_RESULT_DIR
    bounded = _bounded_context(context)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_UNSAVED, verdict.reason)
    artifact = withheld_review_path(state_dir, task_id)
    payload = {
        "schema_version": 2,
        "review_id": withheld_review_id(task_id),
        "status": "pending_dm",
        "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "task_id": task_id,
        "agent_id": agent_id,
        "reason": verdict.reason,
        "context": bounded,
        "withheld_body": body,
    }
    try:
        saved = _write_artifact(artifact, payload)
    except Exception:  # noqa: BLE001 — storage failure must remain fail-closed
        saved = False
    if not saved:
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_UNSAVED, verdict.reason)
    return TeamResultVerdict(
        VERDICT_SUPPRESS, "[no-send]", f"{verdict.reason}; pending private owner review")


def suppressed_record_path(state_dir: Path, task_id: str) -> Path:
    return Path(state_dir) / SUPPRESSED_RESULT_DIR / f"{withheld_review_id(task_id)}.json"


def materialize_suppressed_verdict(verdict: TeamResultVerdict, body: str,
                                   state_dir: Path, task_id: str, context=None,
                                   agent_id: str = "", now=None, *,
                                   stub: str) -> TeamResultVerdict:
    """Realise a notice-bearing SUPPRESS as a journaled close carrying `stub`.

    The record requirement is the policy, not the notice: once the suppression
    is durably journaled, posting prose about marker mechanics into a human
    channel is noise. `stub` must come from suppression_stub_for_tier -- it is
    the canonical, sender-uninfluenced body, and it preserves a dedup target
    that a flat "[no-send]" would drop. Fail-CLOSED -- if the journal cannot be
    written the notice stands, so the record is never silently dropped.
    """
    if verdict.kind != VERDICT_SUPPRESS or verdict.body != TEAM_SUPPRESS_RESULT:
        return verdict                     # already silent, or not a suppress
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / SUPPRESSED_RESULT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        return verdict
    payload = {
        "schema_version": 1,
        "record_id": withheld_review_id(task_id),
        "status": "suppressed_silent_close",
        "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "task_id": task_id,
        "agent_id": agent_id,
        "reason": verdict.reason,
        "context": _bounded_context(context),
        "suppressed_body": body,
    }
    try:
        saved = _write_artifact(suppressed_record_path(state_dir, task_id), payload)
    except Exception:  # noqa: BLE001 -- storage failure must remain fail-closed
        saved = False
    if not saved:
        return verdict
    return TeamResultVerdict(
        VERDICT_SUPPRESS, stub, f"{verdict.reason}; journaled silent close")


def classify_result_for_tier(body: str, tier, repo: Path,
                             secret_filter=None,
                             scan_sensitive_data: bool = True) -> TeamResultVerdict:
    """The guard-owned policy verdict. Adapters apply transport mechanics only;
    re-deciding (or bypassing) this classification in a bridge is a boundary
    violation, not an implementation choice."""
    if not is_guarded_tier(tier):
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    try:
        return TeamResultVerdict(
            VERDICT_DELIVER,
            scan_team_result(body, repo, secret_filter, scan_sensitive_data),
            None)
    except TeamResultLeakError as exc:
        if str(exc) == "suppressive delivery marker":
            return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT, str(exc))
        if str(exc) == "result delivery control marker":
            return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_MARKER, str(exc))
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT, str(exc))
    except Exception as exc:
        # Scanner unavailable is fail-CLOSED: an unscannable guarded result is
        # withheld, never delivered on the assumption that it was probably fine.
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT,
                                 f"scanner unavailable: {exc}")


def guard_result_for_tier(body: str, tier, repo: Path, secret_filter=None,
                          scan_sensitive_data: bool = True, *,
                          suppress_journal=None):
    """Consumer-facing gate: returns (safe_body, withheld_reason).

    Returns a body rather than raising, so a caller cannot deliver the raw text
    by catching an exception -- the safe body is the only one it is handed.
    Derived from classify_result_for_tier so the verdict has exactly one owner.

    suppress_journal: `(state_dir, task_id)` from an adapter whose surface is a
    human channel. The notice becomes a journaled silent close there; adapters
    that omit it keep the posted notice.
    """
    verdict = classify_result_for_tier(
        body, tier, repo, secret_filter, scan_sensitive_data)
    if suppress_journal is not None:
        # The stub is this module's existing policy; the journal only adds
        # the record. Never mint a body here.
        stub = suppression_stub_for_tier(body, tier)
        if stub is not None:
            state_dir, task_id = suppress_journal
            verdict = materialize_suppressed_verdict(
                verdict, body, state_dir, task_id, stub=stub)
    return verdict.body, verdict.reason

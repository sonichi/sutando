#!/usr/bin/env python3
"""Final scan applied to a Team-tier result before any router reads its markers.

A Team result is produced under a prompt its sender can influence, so the last
thing between that text and a marker-interpreting router must be code, not the
producer's compliance with an instruction.

Ownership: this module is the single implementation of the policy — the control
marker set, the secret scan, and what a withheld result says. Adapters bind
their own tier lookup and delivery; they must not restate any of it.

Suppression markers are deliberately NOT controls here. `[channel:]` and
`[file:]` move data somewhere the sender should not reach; `[no-send]`,
`[REPLIED]` and `[deduped:]` move nothing, and the party left without an answer
is the same non-owner who asked. What they can hide is that a task was handled,
which is an accountability property — so they are honoured on every tier and
journaled, rather than refused.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    "carried a delivery-control marker, which non-owner results may not use. "
    "The owner can review the work locally."
)

TEAM_LEAK_RESULT_UNSAVED = (
    "I completed the Team task, but the response was withheld because it may "
    "contain sensitive information or delivery-control markers. Preparing the "
    "private owner review failed; the result remains withheld and will retry."
)

# Reached ONLY when the record cannot be written: a close must never be both
# silent and untraceable. Unreachable in normal operation.
TEAM_SUPPRESS_RESULT = (
    "Task handled. The agent marked this reply as not-for-delivery, but the "
    "record of that suppression could not be written, so this notice is "
    "delivered in its place. Nothing was withheld for content reasons."
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


def only_task_output_attachments(body: str, task_output_root) -> bool:
    """The task-scoped output allowance: True when EVERY attach marker in `body`
    is a standalone `[file: <absolute path>]` line whose realpath sits under
    `task_output_root`. Aliases, inline mentions and out-of-root paths keep the
    body a delivery-control withhold. Grammar comes from parse_markers, per line.
    """
    root = os.path.realpath(str(task_output_root))
    live = sorted(a.value for a in parse_markers(body or "").actions if a.kind == "attach")
    if not live:
        return True
    # Multiset equality: every LIVE marker must be one of the standalone in-root
    # lines, so a shown (fenced) marker can never stand in for an issued one.
    standalone = []
    for line in (body or "").split("\n"):
        if not line.lstrip().startswith("[file:"):
            continue
        parsed = parse_markers(line)
        if parsed.body.strip() or len(parsed.actions) != 1 or parsed.actions[0].kind != "attach":
            continue
        target = parsed.actions[0].value
        if os.path.isabs(target) and os.path.realpath(target).startswith(root + os.sep):
            standalone.append(target)
    return sorted(standalone) == live


def scan_team_result(body: str, repo: Path, secret_filter=None,
                     scan_sensitive_data: bool = True,
                     allow_attach: bool = False,
                     task_output_root: "Path | None" = None) -> str:
    """Return `body` unchanged, or raise TeamResultLeakError if it must be withheld."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    # dm-only only suppresses a redirect the guard already withholds, and a
    # redirect it suppressed never executes -- neither is a control here.
    if kinds & {"redirect"}:
        raise TeamResultLeakError("result delivery control marker")
    if "attach" in kinds and not allow_attach:
        # Verdict-only exemption: the adapter says whether THIS channel+sender
        # may attach; path authorization stays with the transport allowlist.
        if task_output_root is None or not only_task_output_attachments(body, task_output_root):
            raise TeamResultLeakError("result delivery control marker")
    # Suppression is deliberately absent: redirect and attach move data
    # somewhere the sender should not reach, a skip marker moves nothing.
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


def is_suppression_only(body: str) -> bool:
    """True when every marker the shared parser executes is suppressive.

    Classification, not validation: the guard does not inspect a dedup target,
    because the consumer that dereferences one already rejects an id it cannot
    look up safely. Deciding here would be a second, drifting copy of that rule.
    """
    actions = parse_markers(body or "").actions
    return bool(actions) and all(action.kind == "skip" for action in actions)


VERDICT_DELIVER = "deliver"
VERDICT_LEAK = "leak"
VERDICT_SUPPRESS = "suppress"
# Machine-detectable prefix of the fail-closed scanner-outage reason, so a
# consumer that owns its own transport can map outage (not content) to an error.
SCANNER_UNAVAILABLE = "scanner unavailable"
WITHHELD_RESULT_DIR = "withheld-team-results"
SUPPRESSED_RESULT_DIR = "suppressed-team-results"


class TeamResultVerdict(NamedTuple):
    """kind: deliver | leak | suppress. body: what the adapter delivers.
    SUPPRESS now names only bodies the adapter must post in place of the
    result: the pending-review stub, and the fail-closed unrecorded notice."""
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


def is_attach_only_withhold(body: str) -> bool:
    """True when the ONLY control markers are attachments — the one withhold
    class an owner can release per-instance (redirects are never releasable)."""
    kinds = {action.kind for action in parse_markers(body or "").actions}
    return "attach" in kinds and "redirect" not in kinds


def quarantined_attachment_path(state_dir: Path, task_id: str) -> Path:
    return Path(state_dir) / SUPPRESSED_RESULT_DIR / f"qa_{withheld_review_id(task_id)}.json"


def journal_quarantined_attachment(body: str, state_dir: Path, task_id: str,
                                   now=None) -> bool:
    """Quarantine a withheld attach-only result so an owner word can release it.

    Best-effort by design: the withhold already happened fail-closed upstream;
    this record only enables the later release, so an unwritable record costs
    the release option, never the withhold."""
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / SUPPRESSED_RESULT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        return False
    payload = {
        "schema_version": 1,
        "record_id": withheld_review_id(task_id),
        "task_id": task_id,
        "status": "withheld_attachment_pending",
        "created_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "withheld_body": body,
    }
    try:
        return _write_artifact(quarantined_attachment_path(state_dir, task_id), payload)
    except OSError:
        # best-effort for real: a failed record costs the release option,
        # never the already-decided withhold and never the delivery loop
        return False


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


def journal_suppressed_result(verdict: TeamResultVerdict, body: str,
                              state_dir: Path, task_id: str, context=None,
                              agent_id: str = "", now=None) -> TeamResultVerdict:
    """Record a guarded-tier suppression, then hand the body back unchanged.

    Accountability, not confidentiality: a skip marker moves no data, so the
    guard honours it on every tier and the durable record is what keeps it
    auditable. Fail-CLOSED -- if the record cannot be written the notice takes
    the body's place, so a close is never both silent and unrecorded.
    """
    if verdict.kind != VERDICT_DELIVER:
        return verdict                     # leak/suppress already decided
    timestamp = float(time.time() if now is None else now)
    directory = Path(state_dir) / SUPPRESSED_RESULT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT,
                                 "suppression record unwritable")
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
        return TeamResultVerdict(VERDICT_SUPPRESS, TEAM_SUPPRESS_RESULT,
                                 "suppression record unwritable")
    return verdict


def classify_result_for_tier(body: str, tier, repo: Path,
                             secret_filter=None,
                             scan_sensitive_data: bool = True,
                             allow_attach: bool = False,
                             honor_suppressions: bool = True,
                             task_output_root: "Path | None" = None) -> TeamResultVerdict:
    """The guard-owned policy verdict. Adapters apply transport mechanics only;
    re-deciding (or bypassing) this classification in a bridge is a boundary
    violation, not an implementation choice."""
    if not is_guarded_tier(tier):
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    if honor_suppressions and is_suppression_only(body):
        # Above the scan on purpose: an undelivered body has nothing to leak —
        # marker-honouring adapters only (publishers pass honor_suppressions=False).
        return TeamResultVerdict(VERDICT_DELIVER, body, None)
    try:
        return TeamResultVerdict(
            VERDICT_DELIVER,
            scan_team_result(body, repo, secret_filter, scan_sensitive_data,
                         allow_attach=allow_attach,
                         task_output_root=task_output_root),
            None)
    except TeamResultLeakError as exc:
        if str(exc) == "result delivery control marker":
            return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT_MARKER, str(exc))
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT, str(exc))
    except Exception as exc:
        # Scanner unavailable is fail-CLOSED: an unscannable guarded result is
        # withheld, never delivered on the assumption that it was probably fine.
        return TeamResultVerdict(VERDICT_LEAK, TEAM_LEAK_RESULT,
                                 f"{SCANNER_UNAVAILABLE}: {exc}")


def guard_result_for_tier(body: str, tier, repo: Path, secret_filter=None,
                          scan_sensitive_data: bool = True, *,
                          suppress_journal=None, allow_attach: bool = False,
                          honor_suppressions: bool = True,
                          task_output_root: "Path | None" = None):
    """Consumer-facing gate: returns (safe_body, withheld_reason).

    Returns a body rather than raising, so a caller cannot deliver the raw text
    by catching an exception -- the safe body is the only one it is handed.
    Derived from classify_result_for_tier so the verdict has exactly one owner.

    suppress_journal: `(state_dir, task_id)` from an adapter that can write the
    record. A guarded suppression is journaled there and the marker is honoured;
    an adapter that omits it honours the marker with no record of its own.

    task_output_root: the task's OWN output dir (`<results>/<task_id>`), passed
    only for Signal Room tasks. Standalone `[file:]` markers under it survive;
    every other attach/redirect marker is still withheld as delivery control.
    """
    verdict = classify_result_for_tier(
        body, tier, repo, secret_filter, scan_sensitive_data,
        allow_attach=allow_attach, honor_suppressions=honor_suppressions,
        task_output_root=task_output_root)
    if (suppress_journal is not None and is_guarded_tier(tier)
            and is_suppression_only(body)):
        state_dir, task_id = suppress_journal
        verdict = journal_suppressed_result(verdict, body, state_dir, task_id)
    if (suppress_journal is not None and verdict.kind == VERDICT_LEAK
            and is_attach_only_withhold(body)):
        state_dir, task_id = suppress_journal
        journal_quarantined_attachment(body, state_dir, task_id)
    return verdict.body, verdict.reason

#!/usr/bin/env python3
"""Recovery for a `[deduped: <holder>]` result whose holder never answered.

`result_markers.dedup_decision` decides; this binds that decision to a
workspace and performs the filesystem half, so every adapter keeps only its
own routing and notification.

Returns a `(action, payload)` plan rather than acting on the channel itself:
adapters differ in whether sending is sync or async, and keeping that at the
edge makes the policy testable without a live bridge.
"""
from __future__ import annotations

from pathlib import Path

# This file is bundled verbatim into ag2_sparrow, where its siblings are
# package submodules; in src/ they are flat modules. Support both.
try:  # pragma: no cover - exercised by whichever context imports it
    from .local_task_protocol import find_archived_task, find_result, valid_archive_lookup_id
    from .result_markers import (
        build_requeued_task,
        dedup_cross_channel_target,
        dedup_cross_sender_target,
        dedup_decision,
        dedup_requeue_count,
        task_channel_id,
        task_source,
        task_user_id,
        parse_markers,
    )
    from .task_archive import find_task_file
except ImportError:  # pragma: no cover - flat src/ import path
    from local_task_protocol import find_archived_task, find_result, valid_archive_lookup_id
    from result_markers import (
        build_requeued_task,
        dedup_cross_channel_target,
        dedup_cross_sender_target,
        dedup_decision,
        dedup_requeue_count,
        task_channel_id,
        task_source,
        task_user_id,
        parse_markers,
    )
    from task_archive import find_task_file

__all__ = [
    "plan_dedup_recovery",
    "classify_holder_delivery",
    "report_disposition",
    "REPORT_TEMPLATE",
    "CROSS_SENDER_TEMPLATE",
    "CROSS_CHANNEL_TEMPLATE",
    "MALFORMED_TEMPLATE",
]

MALFORMED_TEMPLATE = (
    "⚠️ This was folded into another task, but the holder id on the marker is "
    "unusable, so it could not be recovered. It needs a direct answer."
)

REPORT_TEMPLATE = (
    "⚠️ This was folded into `{holder}`, which delivered nothing, and "
    "re-asking didn't recover it. It needs a direct answer."
)

CROSS_SENDER_TEMPLATE = (
    "⚠️ This was folded into `{holder}`, which was asked by someone else, so the "
    "reply went to them. Re-asking didn't recover it. It needs a direct answer."
)


CROSS_CHANNEL_TEMPLATE = (
    "⚠️ This was folded into `{holder}`, whose reply belongs to a different room "
    "or chat. Re-asking didn't recover it. It needs a direct answer here."
)


def _read(path) -> str | None:
    if path is None:
        return None
    try:
        return path.read_text()
    except OSError:
        return None


def classify_holder_delivery(record, body, live: bool) -> str:
    """Separate an existing answer's pending state from its provider receipt."""
    if not body or not isinstance(body, str):
        return "failed" if record else "missing"
    parsed = parse_markers(body)
    if any(a.kind == "skip" for a in parsed.actions):
        if record and record.get("status") == "DELIVERED" and any(
                a.kind == "skip" and a.value == "REPLIED" for a in parsed.actions):
            return "accepted"
        return "missing"
    if any(a.kind == "redirect" for a in parsed.actions):
        return "failed"
    if record and record.get("status") == "DELIVERED":
        return "accepted"
    if record and record.get("status") == "PARKED":
        return "failed"
    return "pending" if live else "failed"


def plan_dedup_recovery(
    results_dir: Path,
    tasks_dir: Path,
    task_id: str,
    holder_id: str | None,
    asking_channel,
    new_task_id: str,
    commit_identity=None,
    channel_dir: str = "",
    holder_delivery_state=None,
) -> tuple[str, str | None]:
    """Decide and perform the filesystem half of dedup recovery.

    Returns one of:
      ``("honour", None)``     — the holder answered; archive as before.
      ``("requeue", new_id)``  — a re-ask was written; route its reply.
      ``("report", message)``  — tell the asker; do not re-ask again.
      ``("defer", None)``      — nothing was changed; retry on a later pass.
      ``("wait", holder)``     — an existing answer awaits gateway acceptance.

    ``commit_identity(new_task_id)`` runs BEFORE the task file is published and
    must return True. A re-ask visible to the watcher without its routing
    committed is executed anyway, so a failed commit would leave a live orphan
    and the next pass would add another.
    """
    holder = (holder_id or "").strip()
    # `find_result` refuses a malformed id, so recovery would read "delivered
    # nothing" and carry these bytes into the re-ask. Reject; never echo them.
    if holder and not valid_archive_lookup_id(holder):
        return "report", MALFORMED_TEMPLATE
    orig_text = _read(find_task_file(Path(tasks_dir), task_id))
    holder_text = _read(find_result(Path(results_dir), holder)) if holder else None

    decision = dedup_decision(holder_text, orig_text)
    holder_task = (_read(find_task_file(Path(tasks_dir), holder)
                         or find_archived_task(Path(tasks_dir), holder)) if holder else None)
    destination = asking_channel or task_channel_id(orig_text)
    cross_channel = dedup_cross_channel_target(destination, holder_task, task_source(orig_text))
    cross_sender = (dedup_cross_sender_target(task_user_id(orig_text), holder_task)
                    if orig_text and (decision == "honour" or holder_delivery_state is not None) else None)
    reason = "cross-channel" if cross_channel else "cross-sender" if cross_sender else "holder-empty"
    template = (CROSS_CHANNEL_TEMPLATE if cross_channel else
                CROSS_SENDER_TEMPLATE if cross_sender else REPORT_TEMPLATE)
    if holder_delivery_state is not None and holder and not (cross_channel or cross_sender):
        state = holder_delivery_state(holder)
        if state != "missing":
            compatible = (orig_text and holder_task and destination
                          and task_source(orig_text) == task_source(holder_task)
                          and task_channel_id(holder_task) == destination
                          and task_user_id(orig_text)
                          and task_user_id(orig_text) == task_user_id(holder_task))
            if not compatible:
                return "report", (
                    "⚠️ The holder's delivery destination or sender could not be verified. "
                    "This question needs a direct answer.")
            if state == "accepted":
                return "honour", None
            if state == "pending":
                return "wait", holder
            return "report", (
                f"⚠️ The existing answer for `{holder}` is retained but its gateway "
                "delivery requires operator recovery. No new answer was requested.")
    if decision == "honour" and not (cross_channel or cross_sender):
        return "honour", None
    if dedup_requeue_count(orig_text) >= 1:
        return "report", template.format(holder=holder)

    if (decision == "requeue" or cross_channel or cross_sender) and orig_text:
        if commit_identity is not None and not commit_identity(new_task_id):
            return "defer", None
        body = build_requeued_task(
            orig_text, new_task_id, dedup_requeue_count(orig_text) + 1,
            destination, holder,
            reason=reason,
            channel_dir=channel_dir,
        )
        try:
            (Path(tasks_dir) / f"{new_task_id}.txt").write_text(body)
        except OSError:
            # Cannot re-ask; fall through to telling the asker rather than
            # silently archiving against a delivery that never happened.
            return "report", template.format(holder=holder)
        return "requeue", new_task_id

    return "report", template.format(holder=holder)


def report_disposition(action: str, delivered=None) -> str:
    """Is this exchange terminal, given what the adapter's send actually did?

    ``delivered`` is the adapter's own outcome for the send the plan asked for:
    ``True`` confirmed, ``False`` refused/failed, ``None`` unknown. Only the
    ``report`` action consults it — the other actions do not send.

    Returns ``"archive"`` (retire task + result) or ``"retain"`` (leave both in
    place so a later pass retries). An unrecognised action retains: this decides
    whether an unanswered request survives, so the unknown case fails closed.

    Separated from ``plan_dedup_recovery`` because the plan is made before the
    send and this is decided after it; every adapter owns the send and none of
    them should own the rule for what a failed one means.
    """
    if action == "report":
        return "archive" if delivered is True else "retain"
    if action in ("honour", "requeue"):
        return "archive"
    return "retain"

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
    from .local_task_protocol import find_result
    from .result_markers import (
        build_requeued_task,
        dedup_decision,
        dedup_requeue_count,
    )
    from .task_archive import find_task_file
except ImportError:  # pragma: no cover - flat src/ import path
    from local_task_protocol import find_result
    from result_markers import (
        build_requeued_task,
        dedup_decision,
        dedup_requeue_count,
    )
    from task_archive import find_task_file

__all__ = ["plan_dedup_recovery", "REPORT_TEMPLATE", "DEFER"]

# Adapters compare against this rather than the literal: a defer that an
# adapter cannot name is one it archives through, losing route and result.
DEFER = "defer"

REPORT_TEMPLATE = (
    "⚠️ This was folded into `{holder}`, which delivered nothing, and "
    "re-asking didn't recover it. It needs a direct answer."
)


# Present-but-unreadable is not missing: an answer may land a moment later, so
# a terminal decision now would re-ask a question that is about to be answered.
UNREADABLE = object()


def _read(path):
    if path is None:
        return None
    try:
        body = path.read_text()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, so a torn holder escaped `except
        # OSError`; errors="replace" would decode it into a false non-skip answer.
        return UNREADABLE if path.exists() else None
    # Empty decodes cleanly and means the holder delivered nothing, so the
    # question is re-asked; a torn holder fails to decode and defers above.
    return body


def plan_dedup_recovery(
    results_dir: Path,
    tasks_dir: Path,
    task_id: str,
    holder_id: str | None,
    asking_channel,
    new_task_id: str,
    commit_identity=None,
) -> tuple[str, str | None]:
    """Decide and perform the filesystem half of dedup recovery.

    Returns one of:
      ``("honour", None)``     — the holder answered; archive as before.
      ``("requeue", new_id)``  — a re-ask was written; route its reply.
      ``("report", message)``  — tell the asker; do not re-ask again.
      ``("defer", None)``      — nothing was changed; retry on a later pass.

    ``commit_identity(new_task_id)`` runs BEFORE the task file is published and
    must return True. A re-ask visible to the watcher without its routing
    committed is executed anyway, so a failed commit would leave a live orphan
    and the next pass would add another.
    """
    holder = (holder_id or "").strip()
    orig_text = _read(find_task_file(Path(tasks_dir), task_id))
    holder_text = _read(find_result(Path(results_dir), holder)) if holder else None
    if orig_text is UNREADABLE or holder_text is UNREADABLE:
        return "defer", None

    decision = dedup_decision(holder_text, orig_text)
    if decision == "honour":
        return "honour", None

    if decision == "requeue" and orig_text:
        if commit_identity is not None and not commit_identity(new_task_id):
            return "defer", None
        body = build_requeued_task(
            orig_text, new_task_id, dedup_requeue_count(orig_text) + 1,
            asking_channel, holder, reason="holder-empty",
        )
        try:
            (Path(tasks_dir) / f"{new_task_id}.txt").write_text(body)
        except OSError:
            # Cannot re-ask; fall through to telling the asker rather than
            # silently archiving against a delivery that never happened.
            return "report", REPORT_TEMPLATE.format(holder=holder)
        return "requeue", new_task_id

    return "report", REPORT_TEMPLATE.format(holder=holder)

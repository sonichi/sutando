"""Task/result archive policy and claimed-task locator (#933).

claim_task.py (#884) renames task-{id}.txt → task-{id}.claimed-core-N.txt
when a core claims work. Bridge archive calls that hard-code the bare
task-{id}.txt path silently no-op after claiming, leaving stranded
.claimed-core-N.txt files in tasks/ forever.

Usage:
    from task_archive import find_task_file

    task_file = find_task_file(TASKS_DIR, task_id)
    if task_file:
        archive_file(task_file, "tasks", task_id)
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional


def find_task_file(tasks_dir: Path, task_id: str) -> Path | None:
    """Return the actual task file path for task_id, or None if absent.

    Checks the bare name first (unclaimed), then globs for the claimed
    variant (task-{id}.claimed-core-N.txt). If multiple claimed variants
    exist (shouldn't happen but defensive), returns the first lexicographic
    match and that's good enough — the caller only needs one path to archive.
    """
    bare = tasks_dir / f"{task_id}.txt"
    if bare.exists():
        return bare
    matches = sorted(tasks_dir.glob(f"{task_id}.claimed-core-*.txt"))
    return matches[0] if matches else None


def archive_file(
    src: Path,
    kind: str,
    task_id: str,
    archive_tasks_dir: Path,
    archive_results_dir: Path,
    *,
    now: Optional[datetime] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> bool:
    """Move a live task/result into its monthly archive.

    Archival is cleanup, not a delivery gate. A failed move therefore removes
    the stale live source, matching the adapters' established fail-open policy.
    ``False`` means the source was absent or the move failed.
    """
    # Validate BEFORE the try. `base = tasks if kind == "tasks" else results`
    # routed every other value — "task", "Tasks", a typo — silently to results,
    # misfiling the archive rather than raising. Latent today (all three
    # adapters pass literals) but this is shared policy with three callers and
    # gravity toward more (review nit, @sonichi #2505).
    #
    # OUTSIDE the try on purpose: the fail-open handler below catches
    # Exception, so a guard placed inside it is swallowed and ALSO unlinks the
    # source — turning a caller's typo into silent data loss. A programming
    # error is not the failure mode fail-open exists for.
    if kind not in ("tasks", "results"):
        raise ValueError(f"archive_file: kind must be 'tasks' or 'results', got {kind!r}")
    if not src.exists():
        return False
    try:
        month = (now or datetime.now()).strftime("%Y-%m")
        base = archive_tasks_dir if kind == "tasks" else archive_results_dir
        destination_dir = base / month
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(destination_dir / f"{task_id}.txt"))
        return True
    except Exception as exc:
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass
        return False

"""Task-file locator + shared archive helper (#1335 sub-PR-1).

Two primitives:

- ``find_task_file(tasks_dir, task_id)`` — locator that handles the
  ``.claimed-core-N`` rename written by ``claim_task.py`` (#884). Bridge
  archive calls that hard-code ``task-{id}.txt`` silently no-op after
  claiming, leaving stranded files in ``tasks/`` forever.

- ``archive_file(src, kind, task_id, base)`` — move ``src`` into
  ``<base>/<kind>/archive/<YYYY-MM>/<task_id>.txt``. Replaces the
  duplicated impls previously in ``src/discord-bridge.py`` and
  ``src/telegram-bridge.py``. The TypeScript counterpart is
  ``src/task-archive.ts:archiveFile``.

The TypeScript and Python implementations share the behavioral contract
documented in ``docs/bridge-helpers-design.md`` (sub-PR-1 section). A
parity test at ``tests/task-archive-parity.test.py`` exercises both
implementations against the same fixtures.

Usage::

    from task_archive import find_task_file, archive_file

    task_file = find_task_file(TASKS_DIR, task_id)
    if task_file:
        archive_file(task_file, "tasks", task_id, base=WORKSPACE)
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


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
    kind: Literal["tasks", "results"],
    task_id: str,
    base: Path,
) -> None:
    """Move ``src`` to ``<base>/<kind>/archive/<YYYY-MM>/<task_id>.txt``.

    Silent no-op if ``src`` does not exist. On any move failure, falls back
    to ``unlink(missing_ok=True)`` so callers never leave stale task/result
    files behind. Logs failures to stderr (Chi's 2026-04-18 ask: "instead
    of deleting we should archive the tasks. It can be useful for
    self-improving").

    Contract: see ``docs/bridge-helpers-design.md`` § task-archive helper.
    Cross-language parity test: ``tests/task-archive-parity.test.py``.
    """
    try:
        if not src.exists():
            return
        ym = datetime.now(timezone.utc).strftime("%Y-%m")
        dest_dir = base / kind / "archive" / ym
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / f"{task_id}.txt"))
    except Exception as exc:
        print(
            f"archive_file({kind}, {task_id}) failed: {exc}",
            flush=True,
        )
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass

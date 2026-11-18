#!/usr/bin/env python3
"""Find a task's file or result by id wherever it lives: its current home first
(`connections.task_home`), then only the ARCHIVES of the folders its worker used before
switching between own and shared ones (`connections.task_other_home`). A live file in that
other home is never read as current."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_task_protocol as ltp  # noqa: E402
from connections import task_home, task_other_home  # noqa: E402

from delivery.readiness import read_ready_result  # noqa: E402


def find_task(workspace, task_id: str) -> Optional[Path]:
    """The task's file: live or archived in its home, else archived in its other home."""
    hit = ltp.find_archived_task(task_home(task_id, Path(workspace))["tasks"], task_id)
    return hit if hit is not None else find_other_archived_task(workspace, task_id)


def result_candidates(workspace, task_id: str) -> Iterator[Path]:
    """Every existing result for the task: its home's (live, then archives), then its other home's archives."""
    yield from ltp.iter_result_candidates(task_home(task_id, Path(workspace))["results"], task_id)
    other = task_other_home(task_id, Path(workspace))
    if other:
        yield from ltp.iter_result_candidates(other["results"], task_id, include_live=False)


def find_result(workspace, task_id: str) -> Optional[Path]:
    return next(result_candidates(workspace, task_id), None)


def find_ready_result(workspace, task_id: str, *, reader=read_ready_result) -> Optional[Path]:
    """The first result whose body `reader` accepts, in `result_candidates` order."""
    return next((p for p in result_candidates(workspace, task_id) if reader(p) is not None), None)


def find_other_archived_ready_result(workspace, task_id: str, *, reader=read_ready_result) -> Optional[Path]:
    """A ready result in the other home's archives only, for a caller that already searched the home."""
    other = task_other_home(task_id, Path(workspace))
    if not other:
        return None
    return next((p for p in ltp.iter_result_candidates(other["results"], task_id, include_live=False)
                 if reader(p) is not None), None)


def find_other_archived_result(workspace, task_id: str) -> Optional[Path]:
    """Any existing result in the other home's archives only, ready or not."""
    other = task_other_home(task_id, Path(workspace))
    return next(ltp.iter_result_candidates(other["results"], task_id, include_live=False), None) if other else None


def find_other_archived_task(workspace, task_id: str) -> Optional[Path]:
    """The task's file in the other home's archives only, for a caller that already searched the home."""
    other = task_other_home(task_id, Path(workspace))
    return ltp.find_archived_task(other["tasks"], task_id, include_live=False) if other else None

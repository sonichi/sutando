#!/usr/bin/env python3
"""Naming and moves for `results/undelivered/` — the delivery quarantine.

The convention (`<stem>-<epoch>.txt`) had exactly one owner, the sparrow bridge,
and only one direction: in. Operator recovery needs the way back, and a second
copy of the name format in a second module is how the two drift. So the format
lives here once and both directions are derived from it.

Dependency-light on purpose: no bridge import, no gateway, no env. The caller
supplies the results directory.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

DIRNAME = "undelivered"
# `<task stem>-<unix seconds>.txt`; the stem may itself contain hyphens.
_QUARANTINED = re.compile(r"^(?P<stem>.+)-(?P<epoch>\d+)\.txt$")


def quarantine_dir(results_dir: Path) -> Path:
    return Path(results_dir) / DIRNAME


def quarantine_name(stem: str, when: Optional[float] = None) -> str:
    """The one place the quarantined filename is spelled."""
    return f"{stem}-{int(when if when is not None else time.time())}.txt"


def quarantine(rfile: Path, results_dir: Path,
               when: Optional[float] = None) -> Path:
    """Move a refused result out of the drain's view. Returns the new path."""
    d = quarantine_dir(results_dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / quarantine_name(Path(rfile).stem, when)
    Path(rfile).rename(target)
    return target


def find_quarantined(results_dir: Path, task_id: str) -> list[Path]:
    """Every quarantined copy of one task, oldest first.

    A task can be quarantined more than once (each failed recovery adds one), so
    the caller decides which to restore rather than being handed a guess.
    """
    d = quarantine_dir(results_dir)
    if not d.is_dir():
        return []
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    out = []
    for p in d.glob("*.txt"):
        m = _QUARANTINED.match(p.name)
        if m and m.group("stem") == stem:
            out.append((int(m.group("epoch")), p))
    return [p for _, p in sorted(out)]


def restore(results_dir: Path, task_id: str) -> Optional[Path]:
    """Return the NEWEST quarantined body to the drain's canonical name.

    Returns the restored path, or None when nothing was quarantined. Refuses to
    overwrite a live result: a newer reply already waiting to go is the one the
    user should get, not a resurrected older one.
    """
    found = find_quarantined(results_dir, task_id)
    if not found:
        return None
    stem = f"task-{task_id}" if not str(task_id).startswith("task-") else str(task_id)
    target = Path(results_dir) / f"{stem}.txt"
    if target.exists():
        return None
    found[-1].rename(target)
    return target

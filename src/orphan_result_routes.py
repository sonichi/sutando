"""Routes for results whose task a bridge never saw.

A bridge delivers by looking up an in-memory map it fills when IT receives the
inbound message. A task file written straight into `tasks/` by a cron, a script
or another host is absent from that map, so its result is written, never read,
and never errors — the `channel_id:` the producer declared is inert.

This module answers only "which destination did the task file declare, and is
it safe for this transport to use it". Delivery, claiming and marker handling
stay with the adapter, which injects its own id validator because a Discord
snowflake, a Telegram chat id and a Matrix room id are not interchangeable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).parent))
from local_task_protocol import parse_task_headers  # noqa: E402
from task_archive import find_task_file  # noqa: E402

# Transports that own their own delivery loop. Their ids can coincidentally
# satisfy another transport's shape, so exclude by declared source first.
FOREIGN_SOURCES = frozenset({"telegram", "slack", "ag2space", "phone", "voice"})

# A cap, not a target: the scan runs every poll tick and a results/ backlog
# must not turn one tick into an unbounded directory walk.
DEFAULT_LIMIT = 25


def _find_task_file(task_id: str, tasks_dir: Path, archive_dir: Path) -> Path | None:
    """Live file via the shared finder (it also matches `.claimed-core-N`), then
    the archive — by result time the core has usually already archived the task."""
    live = find_task_file(tasks_dir, task_id)
    if live is not None:
        return live
    try:
        # rglob, not a fixed month dir: the archive layout is the core's to choose.
        return next(archive_dir.rglob(f"{task_id}.txt"), None)
    except OSError:
        return None


def orphan_result_routes(
    results_dir: Path,
    tasks_dir: Path,
    archive_tasks_dir: Path,
    known_ids: Iterable[str],
    is_valid_channel_id: Callable[[str], bool],
    limit: int = DEFAULT_LIMIT,
) -> dict[str, str]:
    """Map task_id -> declared channel_id for undeliverable results.

    Every skip is deliberate: no task file means no declared destination, and
    guessing one would post a private body to whatever channel was handy.
    """
    known = set(known_ids)
    routes: dict[str, str] = {}
    try:
        # scandir, not glob: glob swallows a directory-level EACCES and yields
        # nothing, so an unreadable results/ would look like a clean backlog.
        entries = sorted(
            (p for p in results_dir.iterdir()
             if p.name.startswith("task-") and p.suffix == ".txt"),
            key=lambda p: p.name,
        )
    except OSError:
        return routes

    for path in entries:
        if len(routes) >= limit:
            break
        task_id = path.stem
        if task_id in known:
            continue
        task_file = _find_task_file(task_id, tasks_dir, archive_tasks_dir)
        if task_file is None:
            continue
        try:
            headers = parse_task_headers(task_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if (headers.get("source") or "").strip().casefold() in FOREIGN_SOURCES:
            continue
        channel_id = (headers.get("channel_id") or "").strip()
        if not channel_id or not is_valid_channel_id(channel_id):
            continue
        routes[task_id] = channel_id
    return routes

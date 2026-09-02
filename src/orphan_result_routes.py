"""Routes for results whose task a bridge never saw.

A bridge delivers by looking up an in-memory map it fills when IT receives the
inbound message. A task file written straight into `tasks/` by a cron, a script
or another host is absent from that map, so its result is written, never read,
and never errors — the `channel_id:` the producer declared is inert.

This module answers only "which destination did the task file declare, and is
it safe for this transport to use it". Delivery, claiming and marker handling
stay with the adapter, which injects its own id validator because a Discord
snowflake, a Telegram chat id and a Matrix room id are not interchangeable.

The bound is on candidates EXAMINED, not on routes returned: the caller polls
every second, so an unroutable backlog would otherwise re-walk the archive on
every tick. A cursor makes the scan round-robin so nothing starves.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).parent))
from local_task_protocol import find_archived_task, parse_task_headers  # noqa: E402
from task_archive import find_task_file  # noqa: E402

# Transports that own their own delivery loop. Their ids can coincidentally
# satisfy another transport's shape, so exclude by declared source first.
FOREIGN_SOURCES = frozenset({"telegram", "slack", "ag2space", "phone", "voice"})

# A cap, not a target: the scan runs every poll tick and a results/ backlog
# must not turn one tick into an unbounded directory walk.
DEFAULT_LIMIT = 25


def orphan_result_routes(
    results_dir: Path,
    tasks_dir: Path,
    known_ids: Iterable[str],
    is_valid_channel_id: Callable[[str], bool],
    limit: int = DEFAULT_LIMIT,
    cursor: str = "",
) -> tuple[dict[str, str], str]:
    """(task_id -> declared channel_id, next cursor) for undeliverable results.

    Every skip is deliberate: no task file means no declared destination, and
    guessing one would post a private body to whatever channel was handy.
    """
    known = set(known_ids)
    routes: dict[str, str] = {}
    try:
        # scandir, not glob: glob swallows a directory-level EACCES and yields
        # nothing, so an unreadable results/ would look like a clean backlog.
        entries = sorted(
            p for p in results_dir.iterdir()
            if p.name.startswith("task-") and p.suffix == ".txt"
        )
    except OSError:
        return routes, cursor
    if not entries:
        return routes, ""

    # Round-robin from the cursor. Bounding successful ROUTES would leave the
    # per-candidate lookup unbounded, and this runs once a second.
    start = next((i for i, p in enumerate(entries) if p.name > cursor), 0)
    order = entries[start:] + entries[:start]

    last = cursor
    for path in order[:limit]:
        last = path.name
        task_id = path.stem
        if task_id in known:
            continue
        # Two helpers, not one: find_task_file is the only one that matches a
        # CLAIMED `<id>.claimed-core-N`; find_archived_task is the measured walk.
        # Both stat the tree, so an unreadable dir raises here on some Python
        # versions and returns None on others; the caller polls every second, so
        # one bad directory must skip its item rather than kill the loop.
        try:
            task_file = find_task_file(tasks_dir, task_id) or find_archived_task(tasks_dir, task_id)
        except OSError:
            continue
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
    return routes, ("" if last == entries[-1].name else last)

"""Owner-reply plumbing for a launcher gate that runs while no core is up to read the owner's DM.

A gate that refuses to start the core can still talk to the owner. It queues one
DM under a fixed `results/proactive-*.txt` name, reads the owner's bare reply
from `tasks/`, answers that reply in its own conversation, and archives it so a
core that comes up later never acts on it a second time.

A caller records each reply it takes and the steps it finished (`STEPS`), each
persisted as it lands, so a crash replays only what did not finish. The record
decides whether an answer was written, never the live result file, and an
answer already delivered and archived by a bridge is never sent again.

This module holds only that shape, so a second gate reuses it instead of copying
it. The decision of what a reply means stays with the caller.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Tuple

_TRAILING = re.compile(r"[\s.!]+$")
STEPS = ("applied", "answered", "archived")


def _ws(workspace: Optional[Path]) -> Path:
    from workspace_default import resolve_workspace
    return Path(workspace) if workspace else Path(resolve_workspace())


def normalize(text: str) -> str:
    """Lower case, inner spaces collapsed, trailing punctuation dropped."""
    return _TRAILING.sub("", " ".join((text or "").split()).lower())


def owner_reply_text(path: Path) -> Optional[str]:
    """The reply body of an owner-tier task, without quoted reply context; else None."""
    from local_task_protocol import parse_task_headers_lenient, split_reply_context
    from policy.egress.result import resolve_access_tier
    if resolve_access_tier(path) != "owner":
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return split_reply_context(parse_task_headers_lenient(text).body)[1].strip()


def matching_replies(workspace: Optional[Path], phrases: Iterable[str],
                     skip: Iterable[str] = ()) -> Iterator[Tuple[Path, str]]:
    """(task, phrase) for each unanswered owner task whose whole message is one phrase."""
    wanted = {normalize(p): p for p in phrases}
    skip = set(skip)
    workspace = _ws(workspace)
    from local_task_protocol import find_result
    for path in sorted((workspace / "tasks").glob("task-*.txt")):
        if path.stem in skip or find_result(workspace / "results", path.stem) is not None:
            continue
        body = owner_reply_text(path)
        if body is not None and normalize(body) in wanted:
            yield path, wanted[normalize(body)]


def queue_notice(workspace: Optional[Path], name: str, body: str) -> bool:
    """Write one owner DM under a fixed results/ name. A retry rewrites the same name."""
    from local_record import write_text_whole
    from result_markers import neutralize_markers
    try:
        write_text_whole(_ws(workspace) / "results" / name, "[dm-only]\n" + neutralize_markers(body))
    except OSError:
        return False
    return True


def finish(workspace: Optional[Path], entry: dict, mark: Callable[[str], None]) -> None:
    """Answer, then archive, one applied reply; `mark(step)` persists each step as it lands.
    An unmarked answer may have been written just before a crash, so any copy counts."""
    from local_record import write_text_whole
    from local_task_protocol import find_result
    from task_archive import archive_file
    workspace = _ws(workspace)
    task = entry["task"]
    if not entry.get("answered"):
        if find_result(workspace / "results", task) is None:
            write_text_whole(workspace / "results" / f"{task}.txt", entry["reply"].rstrip("\n") + "\n")
        mark("answered")
    if not entry.get("archived"):
        live = workspace / "tasks" / f"{task}.txt"
        if live.exists():
            archive_file(live, "tasks", task, tasks_dir=workspace / "tasks" / "archive",
                         results_dir=workspace / "results" / "archive", log=lambda _m: None)
        mark("archived")

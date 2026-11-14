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


def _owner_task(path: Path) -> Optional[Tuple[Optional[dict], str, str]]:
    """(quoted message or None, reply body, channel_id) of an owner-tier task, else None."""
    from local_task_protocol import parse_task_headers_lenient, split_reply_context
    from policy.egress.result import resolve_access_tier
    if resolve_access_tier(path) != "owner":
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = parse_task_headers_lenient(text)
    quoted, body = split_reply_context(parsed.body)
    return quoted, body.strip(), (parsed.headers.get("channel_id") or "").strip()


def owner_reply(path: Path) -> Optional[Tuple[Optional[dict], str]]:
    """(the message it quotes, or None; the reply body) of an owner-tier task, else None."""
    got = _owner_task(path)
    return None if got is None else got[:2]


def owner_reply_text(path: Path) -> Optional[str]:
    """The reply body of an owner-tier task, without quoted reply context; else None."""
    got = owner_reply(path)
    return None if got is None else got[1]


def notice_text(body: str) -> str:
    """A queued notice as the owner reads it, normalized: what a reply quoting it carries."""
    from result_markers import neutralize_markers
    return normalize(neutralize_markers(body))


def owner_dm(workspace: Optional[Path]) -> str:
    """The owner's DM room as the gateway last recorded it; "" when unknown."""
    try:
        from owner_room_access import owner_dm as dm, owner_routing
        return dm(owner_routing(_ws(workspace)))
    except Exception:
        return ""


def _belongs(quoted: Optional[dict], channel: str, dm: str, notices: Iterable[str]) -> bool:
    # A quoted reply must quote this incident's notice; a bare one must be in the notice's DM.
    if quoted is not None:
        return normalize(quoted.get("body") or "") in set(notices)
    return bool(dm) and channel == dm


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def unclaimed_task_id(path: Path) -> Optional[str]:
    """The canonical id of a live task no one has claimed or been assigned, else None."""
    from task_archive import task_id_for, task_id_from_filename
    stem = Path(path).name[:-4] if Path(path).name.endswith(".txt") else None
    tid = task_id_for(Path(path))
    if stem is None or tid is None:
        return None
    # A `.claimed-`/`.assigned-` name is held by a core, unless the header declares it as the id.
    return tid if task_id_from_filename(Path(path).name) == stem or tid == stem else None


def matching_replies(workspace: Optional[Path], phrases: Iterable[str],
                     skip: Iterable[str] = (), since: Optional[float] = None,
                     dm: str = "", notices: Iterable[str] = ()
                     ) -> Iterator[Tuple[Path, str, str]]:
    """(task file, task id, phrase) for each unclaimed, unanswered owner task whose whole
    message is one phrase, written at or after `since`, that quotes one of `notices`
    (`notice_text` values) or quotes nothing and was sent in `dm`."""
    wanted = {normalize(p): p for p in phrases}
    skip, notices = set(skip), {n for n in notices if n}
    workspace = _ws(workspace)
    from local_task_protocol import find_result
    for path in sorted((workspace / "tasks").glob("task-*.txt")):
        tid = unclaimed_task_id(path)
        if tid is None or tid in skip or find_result(workspace / "results", tid) is not None:
            continue
        if since is not None and (_mtime(path) or 0) < since:
            continue
        got = _owner_task(path)
        if got is None or normalize(got[1]) not in wanted:
            continue
        if _belongs(got[0], got[2], dm, notices):
            yield path, tid, wanted[normalize(got[1])]


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
        live = workspace / "tasks" / Path(entry.get("file") or f"{task}.txt").name
        if live.exists():
            archive_file(live, "tasks", task, tasks_dir=workspace / "tasks" / "archive",
                         results_dir=workspace / "results" / "archive", log=lambda _m: None)
        mark("archived")

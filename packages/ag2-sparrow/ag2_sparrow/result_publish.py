#!/usr/bin/env python3
"""One publisher for every `results/` body: staged beside the target, fsynced, renamed whole.

A result file is claimed on sight. `Path.write_text` and a shell `>` create the
name empty and fill it afterwards, so a drain woken by the creation reads a
zero-byte file or a prefix, delivers that, and archives the task as answered —
the real body written moments later is a "late duplicate" nobody sends (#3956).

Every in-repo writer of a result body publishes through here; adapters keep
only what to say and where. The staged name starts with a dot and ends in
`.tmp`, so no drain's `*.txt` glob can match it, and it is unique per call so
two publishers of one name never rename each other's bytes. Shell writers use
the CLI: `python3 src/result_publish.py <path>` with the body on stdin.

The CLI refuses a `source: cron` task's result that names no destination and
carries no skip marker: no bridge routes it, a `[channel:]` redirect included, so it would be archived unsent.
Owner-facing cron output goes through a channel-bound result instead (a task
with a destination header, or a proactive result routed to the owner's channel);
`--allow-undeliverable` publishes anyway.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

__all__ = ["STAGED_SUFFIX", "stage_text", "is_staged_for", "publish_staged", "publish_text",
           "undeliverable_reason", "main"]

STAGED_SUFFIX = ".tmp"


def stage_text(path: str | Path, text: str, mode: int = 0o666) -> Path:
    """Write `text` to a fresh sibling of `path` and fsync it; returns the staged file.

    `mode` is filtered by the umask, as `open()` would; pass 0o600 for a private record.

    Staging is separate from publishing because a sidecar the published file
    refers to may have to commit in between (the gateway's task media record).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL on a per-call name: honours the umask like a plain write would (mkstemp forces 0600).
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}{STAGED_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def is_staged_for(name: str, target_name: str) -> bool:
    """True when `name` has the shape `stage_text` gives a staging of `target_name`."""
    prefix = f".{target_name}."
    if not (name.startswith(prefix) and name.endswith(STAGED_SUFFIX)):
        return False
    pid, _, token = name[len(prefix):-len(STAGED_SUFFIX)].partition(".")
    return (pid.isascii() and pid.isdigit() and len(token) == 16
            and all(c in "0123456789abcdef" for c in token))


def publish_staged(tmp: str | Path, path: str | Path) -> Path:
    """Rename a staged file onto `path` and fsync the directory, so the name survives a crash."""
    target = Path(path)
    os.replace(tmp, target)
    if os.name != "nt":
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return target


def publish_text(path: str | Path, text: str, mode: int = 0o666) -> Path:
    """Publish `text` at `path` whole: a reader sees the name absent or complete, never a prefix."""
    tmp = stage_text(path, text, mode)
    try:
        return publish_staged(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _helpers():
    try:
        from . import local_task_protocol, result_markers, task_archive, workspace_default
    except ImportError:
        import local_task_protocol
        import result_markers
        import task_archive
        import workspace_default
    return local_task_protocol, result_markers, task_archive, workspace_default


def undeliverable_reason(path: str | Path, text: str) -> str | None:
    """Why no bridge would deliver `text` published at `path`, or None when one would.

    Fails open: an unknown name, a missing task file or a helper that will not load publishes."""
    try:
        protocol, markers, archive, wsd = _helpers()
    except ImportError:
        return None
    target = Path(path).resolve()
    task_id = archive.task_id_from_filename(target.name)
    if task_id is None:
        return None
    try:
        workspace = wsd.resolve_workspace(migrate=False).resolve()
        if target.parent != workspace / "results":
            return None
        tasks_dir = workspace / "tasks"
        task_file = (archive.find_task_file(tasks_dir, task_id)
                     or protocol.find_archived_task(tasks_dir, task_id))
        if task_file is None:
            return None
        headers = protocol.parse_task_headers(task_file.read_text(encoding="utf-8")).headers
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (headers.get("source") or "").strip().casefold() != "cron":
        return None
    if any((headers.get(k) or "").strip() for k in ("channel_id", "chat_id")):
        return None
    # A [channel:] redirect does not count: no consumer reads a channel-less cron result to see it.
    if markers.has_skip_action(markers.parse_markers(text).actions):
        return None
    return f"{task_id} is a cron task with no channel_id or chat_id"


def main(argv: list[str]) -> int:
    args = argv[1:]
    allow = "--allow-undeliverable" in args
    args = [a for a in args if a != "--allow-undeliverable"]
    if len(args) != 1 or args[0].startswith("-"):
        print("usage: result_publish.py [--allow-undeliverable] <path>   (body on stdin)",
              file=sys.stderr)
        return 2
    text = sys.stdin.read()
    reason = None if allow else undeliverable_reason(args[0], text)
    if reason:
        print(f"result_publish: refused, {reason}: nothing would deliver this body; send it "
              "as a channel-bound result (a task with a destination header, or a "
              "proactive result routed to the owner's channel), start it with [no-send], "
              "or pass --allow-undeliverable", file=sys.stderr)
        return 2
    publish_text(args[0], text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

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
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

__all__ = ["STAGED_SUFFIX", "stage_text", "publish_staged", "publish_text", "main"]

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


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].startswith("-"):
        print("usage: result_publish.py <path>   (body on stdin)", file=sys.stderr)
        return 2
    publish_text(argv[1], sys.stdin.read())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

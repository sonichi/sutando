"""Local JSON records: one object per file under a directory, each written whole in one
rename and read back only when its file name is a safe single path segment that the
record itself names. Generic: the directory, what a record means and which field names
it are the caller's; this module knows file names, atomic writes and containment, nothing
else. A file the caller's naming does not confirm is skipped and named on stderr, never
deleted; deletion removes only a file that resolves inside the directory.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# One path segment: no separator, no whitespace or control character, no leading dot.
NAME_RE = re.compile(r"^(?!\.)[^\s/\\\x00-\x1f\x7f]{1,200}$")


class BadName(ValueError):
    """A record name no path may be built from."""


def safe_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise BadName(f"record name {name!r} is not one path segment of up to 200 printable characters")
    return name


def new_name(prefix: str, now: Optional[float] = None) -> str:
    """Unique per call across processes and hosts' clocks: `<prefix>-<ms>-<pid>-<6 hex>`."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    return safe_name(f"{prefix}-{int(now * 1000)}-{os.getpid()}-{secrets.token_hex(3)}")


def iso(now: Optional[float] = None) -> str:
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_text_whole(path: Path, text: str) -> Path:
    """Appear whole in one rename; a crash mid-write leaves nothing half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_whole(path: Path, record: dict) -> Path:
    return write_text_whole(path, json.dumps(record, ensure_ascii=False, indent=1))


def create_text_whole(path: Path, text: str) -> Path:
    """Appear whole AND only where nothing exists yet: the temp is linked to `path`, which fails
    with FileExistsError instead of replacing a file another writer just published."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


def _default_ident(record: dict):
    return record.get("id")


class RecordDir:
    """The records of one directory. `ident(record)` is the name a record claims; a file
    whose stem is not that name (or not a safe name) is skipped, never deleted."""

    def __init__(self, directory, ident: Callable[[dict], object] = _default_ident):
        self.dir, self.ident = Path(directory), ident

    def path(self, name: str) -> Path:
        return self.dir / f"{safe_name(name)}.json"

    def write(self, name: str, record: dict) -> Path:
        return write_whole(self.path(name), record)

    def contains(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.dir.resolve()) and not path.is_symlink()
        except OSError:
            return False

    def _skip(self, path: Path, why: str) -> None:
        print(f"{self.dir.name}: {path.name} {why}; left in place", file=sys.stderr)

    def read_file(self, path: Path) -> Optional[dict]:
        """The record at `path` when its stem is a safe name the record itself claims."""
        if not NAME_RE.match(path.stem):
            self._skip(path, "is not a record name")
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise TypeError("not an object")
            claimed = self.ident(d)
            if claimed != path.stem:
                raise ValueError(f"names {claimed!r}")
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._skip(path, f"unreadable ({e})")
            return None
        return d

    def files(self) -> list:
        return sorted(p for p in self.dir.glob("*.json") if p.is_file() and self.contains(p)) if self.dir.is_dir() else []

    def entries(self) -> list:
        """(name, record, path) per readable file, by name."""
        out = []
        for p in self.files():
            d = self.read_file(p)
            if d is not None:
                out.append((p.stem, d, p))
        return out

    def delete(self, name: str) -> None:
        p = self.path(name)
        if self.contains(p):
            p.unlink(missing_ok=True)

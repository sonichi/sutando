#!/usr/bin/env python3
"""Readiness of a `results/<task-id>.txt` file, for every delivery consumer.

The single owner of "is this result file ready to send?". Adapters bind their
own resolved results directory and keep only provider-specific delivery; they
must not re-implement the check.

A result path can exist before it holds an answer. The core writes
temp-file-then-rename, but it is an LLM driving a shell and will create the
destination for unrelated reasons, and a partial write can be observed
mid-content. File existence is therefore not readiness: a consumer that treats
it as readiness delivers an empty message and archives the task as done, which
strands the real answer written moments later.

A deliberately empty reply is expressed with the `[no-send]` marker, parsed by
`result_markers`, not by writing an empty file.
"""
from __future__ import annotations

import importlib.util
import time
from pathlib import Path

try:
    from .publication import publish_result
except ImportError:
    # Loaded by file path (tests, standalone tools): bind the sibling directly.
    _spec = importlib.util.spec_from_file_location("publication", Path(__file__).with_name("publication.py"))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    publish_result = _mod.publish_result

__all__ = ["read_ready_result", "is_ready_body", "retire_claim_if_unchanged",
           "sweep_retired"]


def is_ready_body(text: str | None) -> bool:
    """True when `text` is a deliverable body (non-empty after stripping)."""
    return bool(text and text.strip())


def read_ready_result(path: str | Path) -> str | None:
    """Return the stripped body of `path`, or None when it is not ready.

    None covers missing, unreadable and empty-or-whitespace-only files. Callers
    skip on None and retry on a later pass — the file is not consumed, so a
    result that lands between passes is still delivered.
    """
    p = Path(path)
    try:
        body = p.read_text()
    except (OSError, UnicodeDecodeError):
        # Missing, unreadable, or a partial write mid-character. Never
        # deliverable, and readable again on a later pass.
        return None
    body = body.strip()
    return body if body else None


def retire_claim_if_unchanged(claim: str | Path, delivered: str) -> bool:
    """Retire `claim` only while it still holds exactly the body that was sent.

    A claim is a hard link, so a producer holding the original fd keeps
    appending to THIS inode after the consumer read it. A destructive retire
    (unlink) turns any append that lands after the last check into bytes that
    were never guarded and never delivered — and no check-then-unlink closes
    that window, because the producer is not party to the check.

    So retirement never destroys: the claim is MOVED (atomic rename) into a
    sibling `retired/` directory, out of every consumer's glob, and the moved
    inode is re-read. If the body grew in the meantime the move is undone and
    False is returned: the caller releases the claim and a later pass sends it
    whole. An append that lands after that re-read is preserved in `retired/`
    rather than lost. False also covers unreadable, partial or vanished
    claims — never retire what cannot be verified.
    """
    p = Path(claim)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return True
    except OSError:
        # Unreadable: never destroy bytes whose content cannot be verified.
        return False
    try:
        body = raw.decode()
    except UnicodeDecodeError:
        # A partial write mid-character. Bytes EXIST and are undelivered, so
        # this is the opposite of "nothing to retire" — keep the claim.
        return False
    stripped = body.strip()
    if not stripped:
        # Emptied under us: nothing left to retire or resend.
        p.unlink(missing_ok=True)
        return True
    if stripped != delivered:
        return False
    try:
        if p.stat().st_size != len(raw):
            return False
    except OSError:
        return False
    retired = _retired_path(p)
    try:
        retired.parent.mkdir(parents=True, exist_ok=True)
        p.replace(retired)
    except OSError:
        # Vanished or unmovable: release rather than act on a path we cannot verify.
        return False
    try:
        final = retired.read_bytes()
        if final.decode().strip() != delivered:
            retired.replace(p)   # grew between the check and the move: undo, resend whole
            return False
    except (OSError, UnicodeDecodeError):
        try:
            retired.replace(p)
        except OSError:
            pass
        return False
    # Record how much of the inode was delivered: bytes a stale descriptor
    # appends after this point are republished by sweep_retired, not marooned.
    try:
        _delivered_marker(retired).write_text(str(len(final)))
    except OSError:
        pass
    return True


def _delivered_marker(retired: Path) -> Path:
    return retired.with_name(retired.name + ".delivered")


def sweep_retired(results_dir: str | Path, quiesce_s: float = 600.0,
                  now: float | None = None) -> list[Path]:
    """Managed lifecycle for retired inodes: republish any bytes appended
    after retirement as a new proactive file, and drop an inode only once it
    has been quiescent for `quiesce_s`. Returns the republished paths.

    A producer holding the original descriptor can append after the retire
    re-read; the claim protocol cannot prove writer completion, so the
    remainder is delivered as its own message instead of being preserved in
    a directory no consumer reads. A retired file with no marker predates
    this lifecycle: its delivered length is unknown, so it is never
    republished (a duplicate is worse than the loss) and only aged out.
    """
    results = Path(results_dir)
    retired_dir = results / "retired"
    if not retired_dir.is_dir():
        return []
    now = time.time() if now is None else now
    published: list[Path] = []
    for inode in sorted(retired_dir.glob("*.txt")):
        marker = _delivered_marker(inode)
        try:
            raw = inode.read_bytes()
            delivered = int(marker.read_text()) if marker.exists() else None
            mtime = inode.stat().st_mtime
        except (OSError, ValueError):
            continue
        if delivered is not None and len(raw) > delivered:
            remainder = raw[delivered:].decode(errors="replace").strip()
            if remainder:
                target = results / f"proactive-late-{inode.stem}-{int(now)}.txt"
                try:
                    publish_result(target, remainder + "\n")
                except OSError:
                    continue
                published.append(target)
            try:
                marker.write_text(str(len(raw)))
            except OSError:
                pass
            continue
        if now - mtime >= quiesce_s:
            inode.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
    return published


def _retired_path(claim: Path) -> Path:
    """Where a retired claim's inode is kept: a sibling dir no consumer globs."""
    return claim.parent / "retired" / claim.name

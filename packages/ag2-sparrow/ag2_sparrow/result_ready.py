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

from pathlib import Path

__all__ = ["read_ready_result", "is_ready_body", "retire_claim_if_unchanged"]


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
    """Unlink `claim` only while it still holds exactly the body that was sent.

    A claim is a hard link, so a producer holding the original fd keeps
    appending to THIS inode after the consumer read it. Unlinking then destroys
    bytes that were never guarded and never delivered. False means the body
    grew: the caller releases the claim instead, and a later pass sends it whole.
    """
    p = Path(claim)
    current = read_ready_result(p)
    if current is None:
        # Already gone, or emptied under us: nothing left to retire or resend.
        p.unlink(missing_ok=True)
        return True
    if current != delivered:
        return False
    p.unlink(missing_ok=True)
    return True

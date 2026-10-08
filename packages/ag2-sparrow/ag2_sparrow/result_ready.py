#!/usr/bin/env python3
"""Readiness of a `results/<task-id>.txt` file, for every delivery consumer.

The single owner of "is this result file ready to send?". Adapters bind their
own resolved results directory and keep only provider-specific delivery; they
must not re-implement the check.

A result path can exist before it holds an answer. In-repo writers publish
whole through `result_publish`, but the core is an LLM driving a shell and will
create the destination for unrelated reasons, and a `>` redirect fills the file
after creating it. File existence is therefore not readiness: a consumer that
treats it as readiness delivers an empty message or a prefix and archives the
task as done, which strands the real answer written moments later (#3956).

A body is ready only when it is non-empty and holds still: unchanged across the
read, and — when younger than `SETTLE_SEC` — unchanged across a short hold too.

A deliberately empty reply is expressed with the `[no-send]` marker, parsed by
`result_markers`, not by writing an empty file.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["read_ready_result", "is_ready_body", "SETTLE_SEC"]

# A body younger than this may still be growing under a writer that did not publish whole.
SETTLE_SEC = 1.0
_HOLD_SEC = 0.05
_sleep = time.sleep  # bound here: a drain's loop may swap time.sleep for its own tick sentinel


def _shape(st: os.stat_result) -> tuple:
    return (st.st_size, st.st_mtime_ns)


def is_ready_body(text: str | None) -> bool:
    """True when `text` is a deliverable body (non-empty after stripping)."""
    return bool(text and text.strip())


def read_ready_result(path: str | Path) -> str | None:
    """Return the stripped body of `path`, or None when it is not ready.

    None covers missing, unreadable, empty-or-whitespace-only and still-being-
    written files. Callers skip on None and retry on a later pass — the file is
    not consumed, so a result that lands between passes is still delivered.
    """
    p = Path(path)
    try:
        before = p.stat()
        if before.st_size == 0:
            return None
        body = p.read_text()
        after = p.stat()
        if _shape(after) != _shape(before):
            return None  # changed under the read: a prefix, not the body
        if time.time() - after.st_mtime < SETTLE_SEC:
            _sleep(_HOLD_SEC)
            if _shape(p.stat()) != _shape(after):
                return None  # young and still growing
    except (OSError, UnicodeDecodeError):
        # Missing, unreadable, or a partial write mid-character. Never
        # deliverable, and readable again on a later pass.
        return None
    body = body.strip()
    return body if body else None

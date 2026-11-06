"""Reading the workspace state records the runtime-API views project.

These files are written by OTHER processes, so a reader treats them as
untrusted: absent, corrupt, and valid-but-non-object JSON all degrade to
"unknown" rather than raising into a public RPC. `[]` is the case that bites —
it parses cleanly and then AttributeErrors on `.get`, so an exception guard
around `json.loads` alone does not cover it.

That contract already exists at `src/runtime-health.py:_core_status` and
`src/progress_stream.py`; this module is where the runtime-API views get it
instead of each keeping its own copy.

Dependency-light on purpose: stdlib only, no view or server imports.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def read_record(path) -> "dict | None":
    """The object stored at `path`, or None when it cannot be projected.

    None means "unknown" for every reason a caller cannot act on: missing,
    unreadable, malformed, or JSON that is valid but not an object.
    """
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_beat(path, now_fn=time.time) -> dict:
    """A heartbeat payload plus `beatAgeS`, or {} when it cannot be read.

    The age comes from the file's mtime, so a payload that parses but is not an
    object yields {} rather than a record with an age and no fields.
    """
    p = Path(path)
    try:
        age = now_fn() - p.stat().st_mtime
    except OSError:
        return {}
    payload = read_record(p)
    if payload is None:
        return {}
    return {**payload, "beatAgeS": round(age, 1)}

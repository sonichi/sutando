"""Bounded task/message correlation; one locked, atomic sidecar writer."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

try:
    from .file_lock import locked_file
except ImportError:
    from file_lock import locked_file

MAX_ORIGINS = 128
MAX_MESSAGE_ID = 512
MAX_RECORD_BYTES = MAX_ORIGINS * (MAX_MESSAGE_ID * 6 + 4) + 1024


def normalize_origins(values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        if (isinstance(value, str) and value and len(value) <= MAX_MESSAGE_ID
                and value.isprintable() and not any(c.isspace() or c == ',' for c in value)
                and value not in result):
            result.append(value)
            if len(result) == MAX_ORIGINS:
                break
    return result


def origin_header(source_message_id: str) -> str:
    origins = normalize_origins([source_message_id])
    return f"origin_message_ids: {','.join(origins)}\n" if origins else ""


def origins_from_text(text: Optional[str]) -> list[str]:
    try:
        from .local_task_protocol import parse_task_headers
    except ImportError:
        from local_task_protocol import parse_task_headers
    fields = parse_task_headers(text or "").headers
    return normalize_origins([fields.get("source_message_id", ""),
                              *fields.get("origin_message_ids", "").split(',')])


def _path(state_dir: Path, task_id: str) -> Path:
    try:
        from .local_task_protocol import valid_archive_lookup_id
    except ImportError:
        from local_task_protocol import valid_archive_lookup_id
    if not valid_archive_lookup_id(task_id):
        raise ValueError("invalid task id")
    return Path(state_dir) / "task-origins" / f"{task_id}.json"


def read_origins(state_dir: Path, task_id: str) -> list[str]:
    try:
        with _path(state_dir, task_id).open(encoding="utf-8") as stream:
            record = json.loads(stream.read(MAX_RECORD_BYTES + 1))
        values = record.get("origin_message_ids", [])
        return normalize_origins(values) if isinstance(values, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def append_origins(state_dir: Path, task_id: str, values: Iterable[str]) -> bool:
    """Keep the first 128 unique ids. Return False on IO failure; leave delivery decisions unchanged."""
    try:
        path = _path(state_dir, task_id)
        incoming = normalize_origins(values)
        if not incoming:
            return True
        with locked_file(path.with_suffix('.lock')):
            merged = normalize_origins([*read_origins(state_dir, task_id), *incoming])
            fd, name = tempfile.mkstemp(prefix=f'.{task_id}.', suffix='.tmp', dir=path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump({"origin_message_ids": merged}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        return True
    except (OSError, ValueError):
        return False

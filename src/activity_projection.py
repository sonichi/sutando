"""Read-time origin correlation for the existing local activity media projection."""
from __future__ import annotations

import json
import re
from pathlib import Path

from task_origins import normalize_origins, read_origins


_ACTIVITY_FILE = re.compile(
    r"agent-activity(?:\.summaries|\.archive(?:\.\d{4}-\d{2}-\d{2})?)?\.jsonl")


def project_activity_media(path: Path, workspace: Path) -> bytes | None:
    """Return enriched activity bytes, or None when this is ordinary media."""
    if path.parent != (workspace / "state").resolve() or not _ACTIVITY_FILE.fullmatch(path.name):
        return None
    cached, output = {}, []
    for line in path.read_bytes().splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            output.append(line)
            continue
        task = row.get("task") if isinstance(row, dict) else None
        task_id = task.get("id") if isinstance(task, dict) else None
        if isinstance(task_id, str):
            if task_id not in cached:
                cached[task_id] = read_origins(workspace / "state", task_id)
            old = task.get("origins", [])
            origins = normalize_origins([task.get("event", ""),
                                         *(old if isinstance(old, list) else []), *cached[task_id]])
            if origins:
                task["origins"] = origins
                ending = b"\n" if line.endswith(b"\n") else b""
                line = json.dumps(row).encode("utf-8") + ending
        output.append(line)
    return b"".join(output)

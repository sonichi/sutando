"""Observability sinks (Python twin of sink.ts).

The ``JsonlFileSink`` body is the crash-safe O_APPEND / single-write-per-line /
daily-local-date pattern adapted directly from ``src/event_log.py``. A sink MUST
be best-effort (never raise out). ``otlp-http`` is NOT built here -- only the
``Sink`` protocol it will later implement.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Protocol

# `workspace_default` is a flat module at src/; importable because the same src/
# on sys.path that makes the `observability` package importable also resolves it.
from workspace_default import resolve_workspace

__all__ = ["Sink", "JsonlFileSink", "sink_from_config"]


class Sink(Protocol):
    type: str

    def write(self, ev: dict[str, Any]) -> None: ...


class JsonlFileSink:
    type = "jsonl-file"

    def __init__(self, dir: Path | None = None) -> None:
        self._dir = dir

    def write(self, ev: dict[str, Any]) -> None:
        try:
            d = self._dir if self._dir is not None else (resolve_workspace(migrate=False) / "logs")
            d.mkdir(parents=True, exist_ok=True)
            ts = float(ev.get("ts", time.time()))
            date = time.strftime("%Y-%m-%d", time.localtime(ts))
            path = d / f"events-{date}.jsonl"
            line = json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n"
            # Single write() for atomicity on POSIX (event_log.py contract).
            with path.open("ab") as fh:
                fh.write(line.encode("utf-8"))
        except Exception as e:  # noqa: BLE001 — never raise out of a sink
            try:
                print(f"[obs] jsonl-file sink failed: {e}", file=sys.stderr)
            except Exception:
                pass


def sink_from_config(cfg: dict[str, Any]) -> Sink | None:
    t = cfg.get("type")
    if t == "jsonl-file":
        p = cfg.get("path")
        return JsonlFileSink(Path(p) if isinstance(p, str) else None)
    try:
        print(f"[obs] sink type {t!r} not supported yet, skipping", file=sys.stderr)
    except Exception:
        pass
    return None

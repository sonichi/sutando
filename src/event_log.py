"""Structured event log for Sutando — JSONL events for post-mortem debugging.

Writes one JSON object per line to logs/events-YYYY-MM-DD.jsonl. Each event:
    { "ts": <float unix>, "node": <machine id>, "kind": <string>, ... }

Designed for grep-and-jq debugging of incidents like today's PR #331 regret
arc, where the sandbox/bridge/codex interaction needed reconstruction from
unstructured log scraping.

Usage (from any Sutando Python service):

    from event_log import log_event
    log_event("bridge.task_written", task_id="...", tier="team", sender="...")

Event kinds currently supported (non-exhaustive; add as needed):
    bridge.message_received    — Discord/Telegram message arrived
    bridge.tier_classified     — access tier decided
    bridge.task_written        — task file written to tasks/
    bridge.task_dropped        — task filtered out (tier ownership etc.)
    codex.invoked              — codex exec kicked off
    codex.completed            — codex returned
    codex.refused              — codex refused a hostile prompt
    sandbox.violation          — sandbox blocked a read/write attempt
    health.degraded            — a health check flipped to warn/fail
    health.recovered           — a degraded check returned to ok

Fields beyond `ts`, `node`, `kind` are free-form per event kind. Future
tools can filter/aggregate via standard `jq` pipelines without needing a
schema registry; if a kind grows fields over time, readers should accept
missing keys.

No external deps. Crash-safe: writes are appended atomically (single
write() call per line). Concurrent writers may interleave lines but will
not corrupt them on POSIX local filesystems — backed by O_APPEND
semantics + one write() per line, the kernel serializes append offsets
atomically. Verified on APFS with 50 concurrent writers × 2500 events ×
payloads up to 4500 bytes (see notes/event-log-atomicity-test.md).
Pathologically large payloads (> ~1MB) may tear on some filesystems —
keep event payloads modest.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["log_event", "get_log_path", "LOGS_DIR"]

REPO_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_DIR / "logs"

_CACHED_MACHINE: str | None = None


def _machine_id() -> str:
    """Read stand-identity.json[machine] once, cache it. Falls back to
    hostname if identity file is missing."""
    global _CACHED_MACHINE
    if _CACHED_MACHINE is not None:
        return _CACHED_MACHINE
    try:
        identity = REPO_DIR / "stand-identity.json"
        if identity.exists():
            _CACHED_MACHINE = json.loads(identity.read_text()).get("machine", "") or "unknown"
        else:
            import socket
            _CACHED_MACHINE = socket.gethostname().split(".")[0] or "unknown"
    except Exception:
        _CACHED_MACHINE = "unknown"
    return _CACHED_MACHINE


def get_log_path(when: float | None = None) -> Path:
    """Return the daily JSONL file path for a given timestamp (default now).
    Uses local date so files roll at midnight local, matching the rest of
    Sutando's human-readable logging."""
    ts = when if when is not None else time.time()
    date = time.strftime("%Y-%m-%d", time.localtime(ts))
    return LOGS_DIR / f"events-{date}.jsonl"


def log_event(kind: str, **fields: Any) -> None:
    """Append a structured event to today's events JSONL file.

    Never raises — structured logging must not crash the caller. On any
    failure, the event is dropped and a single-line warning goes to stderr.

    Args:
        kind: dotted event identifier, e.g. "bridge.task_written". Freeform.
        **fields: extra event data. Values must be JSON-serializable; anything
                  else is stringified via repr().
    """
    try:
        now = time.time()
        event = {
            "ts": round(now, 3),
            "node": _machine_id(),
            "kind": kind,
        }
        for k, v in fields.items():
            try:
                json.dumps(v)
                event[k] = v
            except (TypeError, ValueError):
                event[k] = repr(v)

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        path = get_log_path(now)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        # Single write() for atomicity on POSIX.
        with path.open("ab") as fh:
            fh.write(line.encode("utf-8"))
    except Exception as e:  # noqa: BLE001 — never crash the caller
        try:
            print(f"event_log: failed to write event {kind}: {e}", file=sys.stderr)
        except Exception:
            pass


if __name__ == "__main__":
    # CLI self-test: emit a hello event, print it back.
    log_event("meta.self_test", hello="world", pid=os.getpid())
    path = get_log_path()
    print(f"wrote to {path}")
    if path.exists():
        print(path.read_text().splitlines()[-1])

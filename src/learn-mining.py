"""learn-mining — pull-at-consumer primitives for the CURATE loop.

The CURATE loop reads local sources growing on their own (logs, tasks/,
results/, CLI conversation JSONL, notes/, memory dir) and emits structured
signals for the LEARN loop to consume. Per the design discussion captured
in `notes/curate-learn-act-self-review-2026-05-05.md`, this module
provides the deterministic primitives the SKILL.md orchestrates.

What lives here (mechanical, testable):
- Cursor management (load / save with atomic write)
- mine_dir: stat-poll a directory, emit events for files newer than cursor
- mine_log: byte-poll a log file, regex-pre-filter, emit events for matches
- mine_jsonl: tail a JSONL file from byte-offset cursor, emit each new line
- audit_memory_pointers: validate MEMORY.md references against actual files
- audit_notes_staleness: list notes with mtime older than threshold
- dedup_against_cooldown: hash-based dedup with TTL window
- DEFAULT_FILTERS: builtin regex set (gap #2 resolution — bootstrap-safe)

What does NOT live here (LLM judgment):
- Pattern recognition over filtered evidence (CURATE's LLM step)
- Action proposing (LEARN)
- Action executing (ACT)

Per `feedback_unit_test_copied_helpers.md`: every primitive has unit tests
in `tests/curate-mining.test.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# DEFAULT_FILTERS — builtin regex set CURATE falls back to when
# `state/learn-filters.json` is missing on first run (gap #2 resolution,
# Chi 2026-05-06). LEARN's `filter_tune` action is the only override path.
#
# Format: list of (log_filename_suffix, kind_label, regex). The `replied`
# pattern is `\bReplied:` not `^Replied:` so a future log-format change
# that prefixes timestamps still matches without a regex update. Per cold
# review on PR #590.
#
# `[skip]` was dropped from the discord-bridge filter set per the
# 2026-05-05 dogfood finding (`notes/curate-dogfood-2026-05-05.md`):
# 365 skip events per 200KB log = 95% noise.
DEFAULT_FILTERS: list[tuple[str, str, str]] = [
    # discord-bridge: routing + outgoing reply confirmations
    ("discord-bridge.log", "replied", r"\bReplied:"),
    ("discord-bridge.log", "error",   r"\b(ERROR|Exception|Traceback|TypeError)\b"),
    # voice-agent: transport health + tool errors
    ("voice-agent.log", "transport", r"transport (1006|1011|1007|connected|closed)"),
    ("voice-agent.log", "error",     r"\b(ERROR|Exception|Traceback)\b"),
    ("voice-agent.log", "delegated", r"\bdelegated\b"),
    # conversation-server: phone call lifecycle
    ("conversation-server.log", "call_event", r"\b(call_started|call_ended|hangup|summary)"),
    ("conversation-server.log", "error",      r"\b(ERROR|Exception|Traceback)\b"),
    # telegram-bridge: same shape as discord
    ("telegram-bridge.log", "replied", r"\bReplied:"),
    ("telegram-bridge.log", "error",   r"\b(ERROR|Exception|Traceback)\b"),
]


# ---------------------------------------------------------------------------
# Cursor management


def load_cursor(state_file: Path) -> dict:
    """Load cursor map from JSON, returning empty dict if missing or
    corrupt. The CURATE pass tolerates a missing cursor (first run) by
    starting fresh. Corrupt JSON is also treated as missing rather than
    crashing the pass — the cost of re-emitting some old events is lower
    than blocking forever on a one-off file corruption."""
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cursor(state_file: Path, cursor: dict) -> None:
    """Atomic write of cursor map. tmp + rename so a concurrent reader
    never sees a partial file. Required because CURATE and LEARN may both
    read the cursor map between passes."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(cursor, indent=2, sort_keys=True))
    tmp.replace(state_file)


# ---------------------------------------------------------------------------
# Directory mining (tasks/, results/)


def mine_dir(
    directory: Path,
    cursor_key: str,
    cursor: dict,
    pattern: str = "*.txt",
    recursive: bool = False,
) -> list[dict]:
    """Stat-poll `directory` for files newer than the cursor's recorded
    freshness. Returns a list of event dicts; mutates `cursor[cursor_key]`
    to the max freshness seen.

    "Freshness" is `max(st_mtime, st_ctime)` — ctime updates on rename, so
    files moved into archive/ via `mv` are detected (mtime alone misses
    these). Same trick PR #586 uses.

    Each event has shape:
        {"source": str, "kind": "file-event", "data": {"path": str,
         "freshness": float}}

    `kind` is mechanical regex-grade; the LLM-driven classification
    (correction / preference / etc.) happens in LEARN, not here.
    """
    events: list[dict] = []
    if not directory.exists():
        return events

    last_seen = float(cursor.get(cursor_key, 0.0))
    new_max = last_seen
    iterator = directory.rglob(pattern) if recursive else directory.glob(pattern)

    for p in iterator:
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        freshness = max(st.st_mtime, st.st_ctime)
        if freshness <= last_seen:
            continue
        events.append({
            "source": str(directory.name),
            "kind": "file-event",
            "data": {"path": str(p), "freshness": freshness},
        })
        if freshness > new_max:
            new_max = freshness

    if new_max > last_seen:
        cursor[cursor_key] = new_max
    return events


# ---------------------------------------------------------------------------
# Log mining (byte-offset cursor + regex pre-filter)


def mine_log(
    log_path: Path,
    cursor: dict,
    filters: list[tuple[str, str, str]] | None = None,
) -> list[dict]:
    """Byte-poll `log_path` from the cursor's recorded offset to EOF.
    Each new line is regex-tested against `filters`; matched lines emit
    an event tagged with the first matching kind label.

    Filter format: list of (log_filename_suffix, kind, regex). Only
    filters whose suffix matches `log_path.name` are applied.

    Returns events of shape:
        {"source": str, "kind": str, "data": {"byte_offset": int,
         "excerpt": str}}

    Cursor key is the absolute log path string. Updates
    `cursor[<path>]` to new EOF offset. On rotation (file size shrinks
    below recorded cursor), emits a `kind="rotated"` event and resets
    cursor to 0 before reading.

    `excerpt` is capped at 250 chars — pointer-style; full context
    recoverable via `{path, byte_offset}`.
    """
    events: list[dict] = []
    if not log_path.exists():
        return events

    if filters is None:
        filters = DEFAULT_FILTERS

    # Compile only the filters whose suffix matches this log file.
    suffix = log_path.name
    applicable = [
        (kind, re.compile(pat))
        for f_suffix, kind, pat in filters
        if suffix == f_suffix or suffix.endswith(f_suffix)
    ]
    if not applicable:
        return events

    cursor_key = str(log_path.resolve())
    offset = int(cursor.get(cursor_key, 0))

    try:
        size = log_path.stat().st_size
    except OSError:
        return events

    # Rotation detection: file shrunk below cursor.
    if size < offset:
        events.append({
            "source": str(log_path.name),
            "kind": "rotated",
            "data": {"byte_offset": offset, "excerpt": f"size {size} < cursor {offset}"},
        })
        offset = 0

    if size <= offset:
        return events

    try:
        with log_path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return events

    text = chunk.decode("utf-8", errors="replace")
    line_offset = offset
    for line in text.splitlines(keepends=True):
        line_len = len(line.encode("utf-8"))
        stripped = line.rstrip("\n\r")
        for kind, pat in applicable:
            if pat.search(stripped):
                events.append({
                    "source": str(log_path.name),
                    "kind": kind,
                    "data": {
                        "byte_offset": line_offset,
                        "excerpt": stripped[:250],
                    },
                })
                break
        line_offset += line_len

    cursor[cursor_key] = size
    return events


# ---------------------------------------------------------------------------
# JSONL mining (Claude Code session transcripts)


def mine_jsonl(
    jsonl_path: Path,
    cursor: dict,
    cursor_subkey: str | None = None,
) -> list[dict]:
    """Tail a JSONL file from a byte-offset cursor. Each new line emits a
    raw event; LEARN classifies content downstream.

    `cursor_subkey` — if provided, the cursor map is treated as nested:
    `cursor[<jsonl_top_key>][cursor_subkey]`. Used for the CLI session
    case where multiple per-UUID cursors live under one top-level key
    (gap #3 resolution: cursor map per UUID).

    Returns events of shape:
        {"source": str, "kind": "jsonl-line",
         "data": {"byte_offset": int, "line_excerpt": str}}
    """
    events: list[dict] = []
    if not jsonl_path.exists():
        return events

    abs_key = str(jsonl_path.resolve())
    if cursor_subkey is None:
        offset = int(cursor.get(abs_key, 0))
    else:
        sub = cursor.get(abs_key, {})
        if not isinstance(sub, dict):
            sub = {}
        offset = int(sub.get(cursor_subkey, 0))

    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return events

    if size < offset:
        # Rotation / truncation — reset.
        offset = 0
        events.append({
            "source": str(jsonl_path.name),
            "kind": "rotated",
            "data": {"byte_offset": offset, "excerpt": "shrunk below cursor"},
        })

    if size <= offset:
        return events

    try:
        with jsonl_path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(size - offset)
    except OSError:
        return events

    text = chunk.decode("utf-8", errors="replace")
    line_offset = offset
    for line in text.splitlines(keepends=True):
        line_len = len(line.encode("utf-8"))
        stripped = line.rstrip("\n\r")
        if stripped:
            events.append({
                "source": str(jsonl_path.name),
                "kind": "jsonl-line",
                "data": {
                    "byte_offset": line_offset,
                    "line_excerpt": stripped[:500],
                },
            })
        line_offset += line_len

    if cursor_subkey is None:
        cursor[abs_key] = size
    else:
        sub = cursor.setdefault(abs_key, {})
        sub[cursor_subkey] = size
    return events


# ---------------------------------------------------------------------------
# Memory + notes audits


def audit_memory_pointers(memory_dir: Path) -> list[dict]:
    """Validate `MEMORY.md` references against actual files in `memory_dir`.

    Returns findings of shape:
        {"kind": "broken-pointer" | "orphan-file", "data": {"file": str}}

    - `broken-pointer`: MEMORY.md references a file that doesn't exist.
    - `orphan-file`: a `.md` file in the dir is not referenced in
      MEMORY.md (excluding `MEMORY.md` itself).

    No mutations — read-only audit. LEARN proposes any cleanup actions;
    ACT executes after owner approval.
    """
    findings: list[dict] = []
    index = memory_dir / "MEMORY.md"
    if not index.exists() or not memory_dir.is_dir():
        return findings

    try:
        content = index.read_text()
    except OSError:
        return findings

    # Match `[Title](file.md)` markdown link references.
    referenced = set(
        m.group(1) for m in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", content)
    )

    on_disk = {p.name for p in memory_dir.glob("*.md") if p.name != "MEMORY.md"}

    for ref in sorted(referenced - on_disk):
        findings.append({"kind": "broken-pointer", "data": {"file": ref}})
    for orphan in sorted(on_disk - referenced):
        findings.append({"kind": "orphan-file", "data": {"file": orphan}})
    return findings


def audit_notes_staleness(notes_dir: Path, threshold_days: int = 30) -> list[dict]:
    """List notes with mtime older than `threshold_days`. No mutations.

    Returns findings of shape:
        {"kind": "stale-note", "data": {"path": str, "age_days": float}}
    """
    findings: list[dict] = []
    if not notes_dir.is_dir():
        return findings

    cutoff_s = time.time() - (threshold_days * 86400)
    for p in notes_dir.glob("*.md"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_s:
            age_days = (time.time() - mtime) / 86400
            findings.append({
                "kind": "stale-note",
                "data": {"path": str(p), "age_days": round(age_days, 1)},
            })
    return findings


# ---------------------------------------------------------------------------
# Cooldown dedup


def hash_finding(parts: Iterable[str]) -> str:
    """Stable 16-char sha256 hash of joined parts. Used as the cooldown
    key for findings — same content → same hash → suppressed within TTL."""
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def dedup_against_cooldown(
    finding_hash: str,
    cooldown_state: dict,
    ttl_h: float,
    now_s: float | None = None,
) -> bool:
    """Return True if `finding_hash` is within the TTL cooldown window
    (suppressed). Side-effect: if NOT suppressed, records the hash with
    the current timestamp into `cooldown_state` for the next pass.

    Per `notes/curate-learn-act-self-review-2026-05-05.md`:
    - 6h TTL for behavioral signals (correction / preference / etc.)
    - 24h TTL for audit findings (drift / staleness)
    - LEARN owns its own 12h cooldown for proposals

    `now_s` parameter exists for testability (inject deterministic time).
    """
    now = now_s if now_s is not None else time.time()
    ttl_s = ttl_h * 3600
    last_seen = cooldown_state.get(finding_hash)
    if last_seen is not None and (now - last_seen) < ttl_s:
        return True
    cooldown_state[finding_hash] = now
    return False


def prune_cooldown(cooldown_state: dict, max_age_h: float = 24.0, now_s: float | None = None) -> int:
    """Drop entries older than `max_age_h` hours. Returns count pruned.
    Keeps the cooldown state file from growing unboundedly across passes.
    """
    now = now_s if now_s is not None else time.time()
    max_age_s = max_age_h * 3600
    stale = [k for k, ts in cooldown_state.items() if (now - ts) > max_age_s]
    for k in stale:
        del cooldown_state[k]
    return len(stale)

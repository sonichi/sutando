"""Task priority taxonomy + readers.

Three-tier enum so writers attach an explicit `priority:` header and consumers
can decide what to process first when more than one task is pending. Keeps the
semantics intentionally coarse — finer-grained scheduling lives in a future
lease-based scheduler; today this is just machine-readable metadata.

Defaults by source (writers emit these; consumers can override per call):
  voice, phone            -> "urgent"   (sub-second response expected)
  chat, context-drop      -> "normal"   (owner foreground)
  discord, telegram (owner-tier)        -> "normal"
  discord, telegram (team/other-tier)   -> "low"
  health-check, sync-memory, cron       -> "low"

Anything not recognized parses as "normal" (fail-open). Order on disk:
"urgent" -> "normal" -> "low" -> unknown -> oldest mtime tiebreak.
"""
from __future__ import annotations
from pathlib import Path
from typing import Iterable, List, Tuple

_ORDER = {"urgent": 0, "normal": 1, "low": 2}
_VALID = frozenset(_ORDER.keys())
_DEFAULT = "normal"


def is_valid_priority(value: str) -> bool:
    """True iff `value` is a recognized priority enum string."""
    return value in _VALID


def default_priority_for_source(source: str, access_tier: str | None = None) -> str:
    """Recommended priority for a given source. Writers should pass the
    `access_tier` (when known) so non-owner channel tasks demote correctly."""
    s = (source or "").lower().strip()
    if s in ("voice", "phone"):
        return "urgent"
    if s in ("chat", "context-drop"):
        return "normal"
    if s in ("discord", "telegram"):
        # Owner-tier traffic stays at normal; team/other gets demoted so a
        # public-channel ping never preempts an owner-DM follow-up.
        return "normal" if (access_tier or "owner").lower() == "owner" else "low"
    if s in ("health-check", "sync-memory", "cron"):
        return "low"
    return _DEFAULT


def parse_priority_from_text(content: str) -> str:
    """Read the `priority:` header from a task-file body. Returns the
    recognized enum string, or "normal" if the header is missing/malformed."""
    for line in content.splitlines():
        line = line.strip()
        if line.lower().startswith("priority:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in _VALID:
                return value
            return _DEFAULT  # malformed -> fail-open to normal
        if line.startswith("---") or line == "":
            # End of header block (heuristic) — stop scanning so we don't
            # match the word "priority" appearing in the task body.
            break
    return _DEFAULT


def parse_priority_from_file(path: Path) -> str:
    """Read priority from a task file. Missing file -> "normal" (fail-open)."""
    try:
        return parse_priority_from_text(path.read_text(errors="replace"))
    except (OSError, UnicodeDecodeError):
        return _DEFAULT


def sort_tasks_by_priority(paths: Iterable[Path]) -> List[Path]:
    """Sort an iterable of task file paths so the highest-priority comes first.
    Tiebreaker: file mtime ascending (oldest first, FIFO within tier)."""
    enriched: List[Tuple[int, float, Path]] = []
    for p in paths:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        prio = parse_priority_from_file(p)
        enriched.append((_ORDER.get(prio, _ORDER[_DEFAULT]), mtime, p))
    enriched.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in enriched]

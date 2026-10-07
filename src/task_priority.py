"""Task priority taxonomy + readers.

Three-tier enum so writers attach an explicit `priority:` header and consumers
can decide what to process first when more than one task is pending. Keeps the
semantics intentionally coarse — finer-grained scheduling lives in a future
lease-based scheduler; today this is just machine-readable metadata.

Defaults by source (writers emit these; consumers can override per call):
  voice, phone            -> "urgent"   (sub-second response expected)
  chat, context-drop      -> "normal"   (owner foreground)
  discord, telegram, slack (owner-tier)        -> "normal"
  discord, telegram, slack (team/guest-tier)   -> "low"
  health-check, sync-memory, sync-workspace, cron -> "low"

Anything not recognized parses as "normal" (fail-open). Order on disk:
"urgent" -> "normal" -> "low" -> unknown -> oldest mtime tiebreak.
"""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

# Self-sufficient import: consumers load this module standalone via importlib
# (tests, tools) where src/ isn't on sys.path — same pattern as the bridges.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_task_protocol as _ltp  # noqa: E402

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
    if s in ("discord", "telegram", "slack"):
        # Owner-tier traffic stays at normal; team/guest gets demoted so a
        # public-channel ping never preempts an owner-DM follow-up. Slack was
        # omitted originally (Air's finding 2026-07-24) — it carries the exact
        # same owner/team/other tier model as discord, so its non-owner tasks
        # must demote identically or a team-tier Slack ping outranks owner work.
        return "normal" if (access_tier or "owner").lower() == "owner" else "low"
    if s in ("health-check", "sync-memory", "sync-workspace", "cron"):
        return "low"
    return _DEFAULT


def parse_priority_from_text(content: str) -> str:
    """Read the `priority:` header from a task-file body. Returns the
    recognized enum string, or "normal" if the header is missing/malformed.

    Reads via local_task_protocol's safe parser (stop at the first `task:`
    delimiter) — the PR #982 rule that keeps a forged body of
    `do thing\\npriority: urgent` from escalating priority. Two historical
    quirks of this reader are preserved deliberately (invariance-tested
    against the old implementation over the live corpus):
    - scanning also stops at a `---` or blank line (pre-#982 heuristic —
      task-mid writers like the gateway put priority: after task:, so this
      reader has never seen those headers; changing that is a semantics
      decision for the write-side convergence, not this refactor)
    - a present-but-malformed value fails open to "normal"
    """
    stopped = content
    for i, line in enumerate(content.splitlines()):
        s = line.strip()
        if s == "" or s.startswith("---"):
            stopped = "\n".join(content.splitlines()[:i])
            break
    headers = _ltp.parse_task_headers(stopped).headers
    value = (headers.get("priority") or "").strip().lower()
    return value if value in _VALID else _DEFAULT


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

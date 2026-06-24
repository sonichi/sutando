"""Progress-streaming helpers for the messaging bridges (issue: Hermes-style
streaming tool output, 2026-06-05).

Pure, side-effect-free helpers so the policy + rendering are unit-testable in
isolation from the async Discord/Telegram bridge that drives them. The bridge
owns message I/O (post + edit); this module only decides *whether* and *what*
to show.

The streamed signal source is the core's ``state/core-status.json`` (the same
file the web dashboard reads) — see CLAUDE.md "Work Status". It is a single,
global status for the one running core, so for the common single-task case the
``step`` reflects the task the user is waiting on. Concurrent tasks share the
status; the placeholder then shows whatever the core is currently doing, which
is acceptable for a progress hint (documented, not a correctness claim).

Python 3.9 compatible (workspace runs system python3 = 3.9.6): future
annotations, no PEP-604 runtime unions, no ``datetime.UTC``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# Default: do not stream until a task has been running this long. Short bursts
# (the overwhelming majority of owner tasks) finish before this and never post
# a placeholder, so the channel stays quiet for quick replies.
DEFAULT_THRESHOLD_S = 8

# Minimum seconds between message edits. Discord/Telegram both rate-limit
# edits; ticking faster than this risks 429s for no perceptible UX gain.
MIN_EDIT_INTERVAL_S = 4

# Hard cap on how long a placeholder may live without a result before the
# bridge should give up and clean it up (prevents a stuck/never-resulting task
# from leaving an orphaned "⏳ working" message forever).
MAX_PLACEHOLDER_AGE_S = 1800  # 30 min


def stream_enabled() -> bool:
    """Master feature flag. Default OFF — the running production bridge is
    untouched until the owner sets ``SUTANDO_PROGRESS_STREAM=1``. This is the
    single switch that gates the whole feature, so a regression can be killed
    instantly by unsetting it, with zero code change."""
    return os.environ.get("SUTANDO_PROGRESS_STREAM", "") == "1"


def should_stream_task(access_tier: Optional[str]) -> bool:
    """Only stream progress for OWNER tasks.

    Non-owner (team/other) tasks are processed in a read-only ``codex`` sandbox
    that does NOT update ``core-status.json``, so there is no live step to show;
    worse, posting a placeholder for them would leak processing state for an
    untrusted sender. ``None`` tier (legacy owner tasks with no field) streams.
    """
    if access_tier is None:
        return True
    return str(access_tier).strip().lower() == "owner"


def read_core_status(state_dir: Path) -> Optional[dict]:
    """Read core-status JSON. Tries ``<state_dir>/core-status.json`` first, then
    the legacy ``<state_dir>/../core-status.json`` (workspace root) — mirroring
    ``status_read_path`` in workspace_default.py so un-migrated installs (where
    the core still writes the workspace-root path) still surface a live step
    instead of a stuck generic "working…". Returns the parsed dict, or None if
    neither is present/valid. Never raises — a malformed status file must not
    break the bridge's poll loop."""
    state_dir = Path(state_dir)
    for p in (state_dir / "core-status.json", state_dir.parent / "core-status.json"):
        try:
            raw = p.read_text().strip()
        except Exception:
            continue
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def current_step(status: Optional[dict]) -> Optional[str]:
    """Extract a human-readable step string from a core-status dict.

    Returns None when the core is idle (no live work to narrate) or when no
    usable ``step`` is present — the caller treats None as "nothing to show",
    leaving any existing placeholder unchanged rather than blanking it.
    """
    if not isinstance(status, dict):
        return None
    if str(status.get("status", "")).strip().lower() == "idle":
        return None
    step = status.get("step")
    if not isinstance(step, str):
        return None
    step = step.strip()
    return step or None


def should_post_placeholder(elapsed_s: float, threshold_s: int = DEFAULT_THRESHOLD_S) -> bool:
    """True once a still-pending task has been running past the threshold."""
    return elapsed_s >= threshold_s


def should_edit(now_s: float, last_edit_s: float, min_interval_s: int = MIN_EDIT_INTERVAL_S) -> bool:
    """Rate-limit guard: True iff enough time has passed since the last edit."""
    return (now_s - last_edit_s) >= min_interval_s


def placeholder_expired(elapsed_s: float, max_age_s: int = MAX_PLACEHOLDER_AGE_S) -> bool:
    """True when a placeholder has lived too long without a result and should
    be cleaned up to avoid an orphaned 'working' message."""
    return elapsed_s >= max_age_s


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"  # ellipsis


def format_progress(step: Optional[str], elapsed_s: float, max_len: int = 180) -> str:
    """Render the live placeholder body.

    Defensive: a missing step still produces a sensible "working" line so the
    user never sees an empty edit. Length-capped so a pathological multi-KB
    ``step`` can't blow past Discord's message limit.
    """
    secs = max(0, int(elapsed_s))
    shown = step if (isinstance(step, str) and step.strip()) else "working…"
    shown = _truncate(shown.strip(), max_len)
    return "⏳ {} ({}s)".format(shown, secs)

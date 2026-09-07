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
import re
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
    """Master feature flag for the owner progress-streamer (both the Discord
    and Telegram bridges consult this). Default OFF.

    Resolution order (the documented CLI > env > config-file precedence):

      1. ``SUTANDO_PROGRESS_STREAM`` env var, if set to a non-empty value —
         wins, so a regression can be killed instantly (``=0``) with no config
         edit. This is the override, not the home.
      2. else ``bridges.progress_stream`` in ``sutando.config.{local.,}json``
         — the durable, per-clone config home (so the toggle isn't a stray env
         var scattered across shell profiles).
      3. else OFF.
    """
    env = os.environ.get("SUTANDO_PROGRESS_STREAM")
    if env is not None and env != "":
        return env == "1"
    # Config-file fallback. Lazy import + defensive sys.path so this works both
    # when the bridge imports us (src/ already on path) and under test loaders
    # that spec-load this file without adding src/ first.
    try:
        import sys
        src_dir = str(Path(__file__).resolve().parent)
        if src_dir not in sys.path:
            sys.path.insert(0, src_dir)
        import sutando_config  # type: ignore[import-untyped]
        val = sutando_config.resolve_progress_stream()
        if val is not None:
            return val
    except Exception:
        pass  # config unreadable → fall through to the safe default (OFF)
    return False


def should_stream_task(access_tier: Optional[str],
                       is_collaborator: bool = False) -> bool:
    """Stream progress for OWNER tasks, and for designated COLLABORATORS.

    The exclusion is about the codex sandbox, not about the tier: a sandboxed
    task never updates ``core-status.json``, so there is no live step to show.
    A collaborator is engaged directly and does update it.
    """
    if access_tier is None:
        # Missing tier field is the platform's owner default (CLAUDE.md).
        return True
    tier = str(access_tier).strip().lower()
    if tier in ("owner", "collaborator"):
        return True
    # The legacy flag is honoured only on the team wire-tier it used to ride;
    # any other tier carrying it is inconsistent input — fail closed.
    return is_collaborator and tier == "team"


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


def step_visible_in(channel_is_private: bool) -> bool:
    """The tier gate answers who SENT the task, never who can SEE the channel, so only
    a known-private chat may publish the step."""
    return bool(channel_is_private)


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


# The exact shape format_progress() emits: a leading hourglass marker, arbitrary
# step text, then a trailing "(Ns)" elapsed counter. A peer node running with
# SUTANDO_PROGRESS_STREAM=1 posts these placeholders while ITS owner task runs;
# in a requireMention:false channel where that node is in allowFrom, our bridge
# would otherwise ingest each placeholder (and each edit) as a fresh task —
# a self-inflicted flood. Detect them so the ingestion gate can drop them.
_PLACEHOLDER_RE = re.compile(r"^\s*⏳ .+ \(\d+s\)\s*$")


def is_progress_placeholder(text: Optional[str]) -> bool:
    """True if ``text`` is a progress-stream placeholder emitted by format_progress().

    Tight-anchored (leading ⏳ marker + trailing ``(Ns)``) so a real owner task
    that merely happens to contain an hourglass emoji is not misclassified and
    silently dropped. Only single-line placeholder bodies match.
    """
    if not isinstance(text, str):
        return False
    return bool(_PLACEHOLDER_RE.match(text))


# ---- outage-aware placeholder (sonichi#2398) -------------------------------
# During the 2026-07-30 outage the placeholder faithfully re-posted a FROZEN
# core-status step for 100 minutes ("SESSION RESTART in flight … (1625s)") —
# the copy claimed progress while the core was dead, and nothing surfaced the
# growing task queue. These helpers let the bridge detect that state and
# switch to honest down-copy. Both signals must agree (frozen status AND stale
# heartbeat) so a legitimately long single step never false-alarms.

# A non-idle core updates core-status.json at every pass/step transition; a
# non-idle status older than this is frozen, not slow.
STATUS_STALE_AFTER_S = 180

# Mirror of the documented .alive liveness threshold (CLAUDE.md "Core
# liveness signal": younger than ~90s → alive).
HEARTBEAT_STALE_AFTER_S = 90


def status_age_s(status: Optional[dict], now_s: float) -> Optional[float]:
    """Seconds since the status dict's ``ts`` stamp; None when unknowable
    (missing/malformed ts) — callers must treat None as 'cannot judge'."""
    if not isinstance(status, dict):
        return None
    ts = status.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return max(0.0, float(now_s) - float(ts))


def status_is_stale(status: Optional[dict], now_s: float,
                    stale_after_s: int = STATUS_STALE_AFTER_S) -> bool:
    """True when a NON-idle status has frozen (ts older than the threshold).

    Idle statuses are never stale (an idle core writes once and rests — that
    is healthy), and an unknowable age is not stale (never cry down on a
    parse gap).
    """
    if current_step(status) is None:
        return False
    age = status_age_s(status, now_s)
    return age is not None and age >= stale_after_s


def heartbeat_is_stale(alive_mtime_s: Optional[float], now_s: float,
                       stale_after_s: int = HEARTBEAT_STALE_AFTER_S) -> bool:
    """True when the newest core .alive file is older than the documented
    liveness threshold — or absent entirely (None), which during an outage is
    exactly the graceful-shutdown/unlinked case."""
    if alive_mtime_s is None:
        return True
    return (float(now_s) - float(alive_mtime_s)) >= stale_after_s


def core_looks_down(status: Optional[dict], alive_mtime_s: Optional[float],
                    now_s: float) -> bool:
    """The #2398 gate: BOTH a frozen non-idle status AND a stale/absent
    heartbeat. A long-running legit step keeps a fresh heartbeat → normal
    copy; an idle-then-killed core has no step to misreport → normal copy."""
    return status_is_stale(status, now_s) and heartbeat_is_stale(alive_mtime_s, now_s)


def format_outage(age_s: Optional[float], queued: int, max_len: int = 300) -> str:
    """Honest down-copy replacing the frozen-step placeholder: how long the
    status has been frozen, how many tasks wait, and the restart remedy."""
    mins = "?" if age_s is None else str(max(1, int(age_s // 60)))
    n = max(0, int(queued))
    body = ("⚠️ core unresponsive — status frozen for {}m; {} task(s) queued, "
            "will process on restart. Restart: Sutando.app → Restart Core, or "
            "`bash src/restart.sh` in a Terminal on the host.").format(mins, n)
    return _truncate(body, max_len)

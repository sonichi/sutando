"""Hang detection, advisory and this adapter's own (#5695 §5, K2): one derivation per tick from the
pane's capture and the Mod's progress record, never from the process.

Two cases: (a) the raw pane static for STALL_MIN_S while the plugin record shows an offer in flight
(CALLING or TURN_CONFIRMED with no result), which is this adapter's rule, not cli_wedge's; (b)
abnormal text on the pane, read line-anchored through the shared banner grammar
(cli_wedge_patterns). The verdict is data the launcher writes into the supervisor record; nothing
here acts.
"""
from __future__ import annotations

import hashlib
import re

from ...cli_wedge_patterns import ABNORMAL_PATTERNS

STALL_MIN_S = 600.0
ABNORMAL_MIN_S = 60.0
INFLIGHT_PHASES = frozenset(("CALLING", "TURN_CONFIRMED"))
_DECOR = re.compile(r"^[\s>·─-╿✓✗✻✳⏵⎿⣀-⣿]+")


def frame_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def abnormal_names(text: str) -> list:
    """The banner families whose pattern leads a line of the capture (prose mid-line never counts)."""
    lines = [_DECOR.sub("", ln) for ln in text.splitlines()]
    return [name for name, rx in ABNORMAL_PATTERNS if any(rx.match(ln) for ln in lines)]


def inflight_task(plugin: dict) -> "str | None":
    """The task whose offer is running with no staged result, else None."""
    for task_id, entry in sorted((plugin.get("inflight") or {}).items()):
        if isinstance(entry, dict) and entry.get("phase") in INFLIGHT_PHASES:
            return task_id
    return None


def derive(previous: dict, text: str, plugin: dict, now: float, stall_min_s: float = STALL_MIN_S,
           abnormal_min_s: float = ABNORMAL_MIN_S) -> dict:
    """The next observation: {frame_hash, static_since, task, abnormal, stall}. `stall` is None or
    {reason, since, frame_hash, task, checks, lines}: `static` once the pane has not changed for
    stall_min_s with an offer in flight, `abnormal:<names>` once abnormal text has shown for
    abnormal_min_s. Any change of frame, task or banner set resets the clock and clears the stall."""
    h = frame_hash(text)
    task = inflight_task(plugin)
    names = abnormal_names(text)
    same = previous.get("frame_hash") == h and previous.get("task") == task
    static_since = previous["static_since"] if same and previous.get("static_since") is not None else now
    abnormal_since = (previous["abnormal_since"] if names and previous.get("abnormal") == names
                      and previous.get("abnormal_since") is not None else (now if names else None))
    out = {"frame_hash": h, "static_since": static_since, "task": task, "abnormal": names,
           "abnormal_since": abnormal_since, "stall": None}
    lines = text.splitlines()[-20:]
    kept = previous.get("stall")
    if kept and kept.get("frame_hash") == h and kept.get("task") == task and kept.get("since") in (static_since, abnormal_since):
        out["stall"] = kept     # the evidence stands: the same stall, so the same token
        return out
    if names and abnormal_since is not None and now - abnormal_since >= abnormal_min_s:
        out["stall"] = {"reason": "abnormal:" + ",".join(names), "since": abnormal_since, "frame_hash": h,
                        "task": task, "checks": "abnormal text on the pane", "lines": lines}
    elif task is not None and now - static_since >= stall_min_s:
        out["stall"] = {"reason": "static for %d s with %s in flight" % (int(now - static_since), task),
                        "since": static_since, "frame_hash": h, "task": task,
                        "checks": "pane static while an offer is in flight", "lines": lines}
    return out

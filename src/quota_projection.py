#!/usr/bin/env python3
"""Quota usage history + even-pace projection series for the dashboard chart.

Owns the quota-history.jsonl writer contract (append, dedup, cap) and the
window-normalized series the chart renders: each reset-to-reset span maps to
one equal-width segment, utilization plotted against the even-pace diagonal so
under-use (below) and over-use (above) read directly off the chart. The
adapter (dashboard.py) resolves the history path and passes the parsed
quota-state dict; nothing here touches workspace resolution or HTTP.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

# Window spans are fixed by the rate limiter, not configurable here.
WINDOW_SPANS = {"5h": 5 * 3600, "7d": 7 * 24 * 3600}
MAX_LINES = 10000  # ~2 weeks at the dashboard's 15s refresh; rewrite keeps the tail

# One writer transaction: the dashboard serves from ThreadingHTTPServer, so
# read/decide/write must not interleave across request threads.
_LOCK = threading.Lock()


def _observation_ts(state: dict) -> float | None:
    """The producer's observation time (`last_checked`), never a reader's clock.

    A dashboard read proves nothing about when quota was measured; only the
    credential proxy's own stamp does. No stamp -> no recordable observation.
    """
    from datetime import datetime, timezone
    raw = state.get("last_checked")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _sample_from_state(state: dict) -> dict | None:
    """Extract one history sample from a quota-state dict; None if unusable."""
    headers = state.get("headers") or {}
    ts = _observation_ts(state)
    if ts is None:
        return None
    try:
        return {
            "ts": ts,
            "u5": float(headers["anthropic-ratelimit-unified-5h-utilization"]),
            "r5": int(headers["anthropic-ratelimit-unified-5h-reset"]),
            "u7": float(headers["anthropic-ratelimit-unified-7d-utilization"]),
            "r7": int(headers["anthropic-ratelimit-unified-7d-reset"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _read_history(history_path: Path) -> list[dict]:
    try:
        lines = history_path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a torn tail line must not poison the series
        if isinstance(rec, dict) and "ts" in rec:
            out.append(rec)
    return out


def record_sample(state: dict, history_path: Path, now: float | None = None) -> bool:
    """Append one sample if it says something new; True when appended.

    Dedup is value-based (same utilizations and resets as the last line), so
    a dashboard polling every 15s does not grow the file while quota stands
    still. The cap rewrite goes through a temp file + os.replace so a reader
    never sees a truncated file.
    """
    sample = _sample_from_state(state)
    if sample is None:
        return False
    with _LOCK:
        history = _read_history(history_path)
        if history:
            last = history[-1]
            if all(last.get(k) == sample[k] for k in ("u5", "r5", "u7", "r7")):
                # Newer stamp + same values = the producer re-measured: advance
                # the stored ts. A reread of the same snapshot is a no-op.
                if float(sample["ts"]) > float(last.get("ts", 0)):
                    history[-1] = {**last, "ts": sample["ts"]}
                    fd, tmp = tempfile.mkstemp(dir=str(history_path.parent))
                    with os.fdopen(fd, "w") as f:
                        f.write("".join(json.dumps(r) + "\n" for r in history))
                    os.replace(tmp, history_path)
                return False
        if len(history) + 1 > MAX_LINES:
            keep = history[-(MAX_LINES - 1):] + [sample]
            fd, tmp = tempfile.mkstemp(dir=str(history_path.parent))
            with os.fdopen(fd, "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in keep))
            os.replace(tmp, history_path)
            return True
        # A crash can leave a newline-less torn tail; appending onto it would
        # concatenate and lose this acknowledged sample on the next read.
        needs_nl = False
        try:
            raw = history_path.read_bytes()
            needs_nl = bool(raw) and not raw.endswith(b"\n")
        except OSError:
            pass
        with history_path.open("a") as f:
            if needs_nl:
                f.write("\n")
            f.write(json.dumps(sample) + "\n")
        return True


def _window_segments(history: list[dict], u_key: str, r_key: str,
                     span: float, now: float, max_windows: int) -> dict:
    """Normalize samples into per-reset segments of equal width.

    Segment k (0-based, oldest first) holds points (x, y): x = fraction of
    that window elapsed at sample time, y = utilization. The even-pace line
    inside every segment is y = x, so above it is over-use, below under-use.
    """
    by_reset: dict[int, list[dict]] = {}
    for rec in history:
        try:
            reset = int(rec[r_key])
            util = float(rec[u_key])
            ts = float(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        start = reset - span
        if not (start <= ts <= reset):
            continue  # a sample outside its own window is clock skew; drop it
        frac = (ts - start) / span
        by_reset.setdefault(reset, []).append({"x": frac, "y": util})
    segments = []
    for reset in sorted(by_reset)[-max_windows:]:
        pts = sorted(by_reset[reset], key=lambda p: p["x"])
        current = reset > now
        seg = {"reset": reset, "current": current, "points": pts}
        if current and pts:
            # Projection extends only from the last VERIFIED observation; a
            # quiet stretch after it is unknown, never rendered as measured.
            last = pts[-1]
            if last["x"] > 0:
                seg["projected_end"] = min(last["y"] / last["x"], 2.0)
        segments.append(seg)
    return {"span_s": span, "segments": segments}


def chart_payload(history_path: Path, now: float, max_windows: int = 4) -> dict:
    """Everything the chart needs, for both windows."""
    history = _read_history(history_path)
    return {
        "now": now,
        "windows": {
            "5h": _window_segments(history, "u5", "r5", WINDOW_SPANS["5h"], now, max_windows),
            "7d": _window_segments(history, "u7", "r7", WINDOW_SPANS["7d"], now, max_windows),
        },
    }

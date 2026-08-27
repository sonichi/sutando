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
import math
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
    from datetime import datetime
    raw = state.get("last_checked")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return ts if math.isfinite(ts) else None


_WINDOW_KEYS = {"5h": ("u5", "r5"), "7d": ("u7", "r7")}
_HDR = "anthropic-ratelimit-unified-{w}-{f}"


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sample_from_state(state: dict) -> dict | None:
    """One history sample; each window parsed INDEPENDENTLY.

    The proxy may write any non-empty header subset, so a valid 5h-only or
    7d-only observation persists its window rather than vanishing. Non-finite
    values invalidate only their own window. No valid window -> no sample.
    """
    headers = state.get("headers") or {}
    ts = _observation_ts(state)
    if ts is None:
        return None
    sample: dict = {"ts": ts}
    for w, (uk, rk) in _WINDOW_KEYS.items():
        u = _finite(headers.get(_HDR.format(w=w, f="utilization")))
        r = _finite(headers.get(_HDR.format(w=w, f="reset")))
        if u is None or r is None:
            continue
        if not (r - WINDOW_SPANS[w] <= ts <= r):
            continue  # an observation outside its own window is inconsistent
        sample[uk], sample[rk] = u, int(r)
    if len(sample) == 1:
        return None
    return sample


def _valid_row(rec: dict) -> dict | None:
    """Canonical form of one stored row, or None if unusable.

    A row needs a finite ts and at least one internally consistent window
    (finite utilization, finite reset, ts inside [reset-span, reset]); an
    inconsistent window is stripped rather than sinking the row.
    """
    if not isinstance(rec, dict):
        return None
    ts = _finite(rec.get("ts"))
    if ts is None:
        return None
    out = {"ts": ts}
    for w, (uk, rk) in _WINDOW_KEYS.items():
        u, r = _finite(rec.get(uk)), _finite(rec.get(rk))
        if u is None or r is None:
            continue
        if not (r - WINDOW_SPANS[w] <= ts <= r):
            continue
        out[uk], out[rk] = u, int(r)
    return out if len(out) > 1 else None


def _canonical_history(history_path: Path) -> tuple[list[dict], bool]:
    """Validated rows in strictly increasing ts, plus a physical-dirty flag.

    dirty=True whenever the file holds anything the canonical view dropped —
    unparseable lines, invalid rows, stripped windows, or order violations —
    so the writer knows a compaction rewrite is owed.
    """
    try:
        lines = history_path.read_text().splitlines()
    except OSError:
        return [], False
    rows: list[dict] = []
    dirty = False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            dirty = True
            continue
        row = _valid_row(rec)
        if row is None:
            dirty = True
            continue
        if isinstance(rec, dict) and set(rec) != set(row):
            dirty = True  # a window was stripped
        if rows and row["ts"] <= rows[-1]["ts"]:
            dirty = True  # out-of-order physical line never joins the canon
            continue
        rows.append(row)
    return rows, dirty


def _read_history(history_path: Path) -> list[dict]:
    """Chart-facing view: the canonical rows only."""
    return _canonical_history(history_path)[0]


def record_sample(state: dict, history_path: Path, now: float | None = None) -> bool:
    """Append one sample if it says something new; True when appended.

    Everything is judged against the CANONICAL history: malformed lines,
    invalid rows, stripped windows and order violations are compacted away
    under the lock the moment any write decision is made. Wire JSON is
    strict (allow_nan=False) end to end.
    """
    sample = _sample_from_state(state)
    if sample is None:
        return False

    def _rewrite(rows):
        fd, tmp = tempfile.mkstemp(dir=str(history_path.parent))
        with os.fdopen(fd, "w") as f:
            f.write("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows))
        os.replace(tmp, history_path)

    def _same(rec):
        keys = [k for pair in _WINDOW_KEYS.values() for k in pair]
        return all(rec.get(k) == sample.get(k) for k in keys)

    with _LOCK:
        history, dirty = _canonical_history(history_path)
        if history:
            last = history[-1]
            if float(sample["ts"]) <= float(last["ts"]):
                # A delayed or re-read snapshot no newer than the canonical
                # tail never lands: ordering is canonical by producer time.
                if dirty:
                    _rewrite(history)
                return False
            if _same(last):
                # Unchanged run: keep its FIRST endpoint, advance its LAST.
                if len(history) >= 2 and _same(history[-2]):
                    history[-1] = {**last, "ts": sample["ts"]}
                    _rewrite(history)
                    return False
                # Only the run's start exists — append its trailing endpoint.
        if dirty:
            _rewrite(history)
        if len(history) + 1 > MAX_LINES:
            keep = history[-(MAX_LINES - 1):] + [sample]
            _rewrite(keep)
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
            f.write(json.dumps(sample, allow_nan=False) + "\n")
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

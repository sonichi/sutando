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

import fcntl
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


# Furthest into the future an observation stamp may plausibly sit. Bounds are
# judged against the caller-injected clock, never a row's own claims.
MAX_FUTURE_SKEW_S = 3600.0


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _sample_from_state(state: dict, now: float) -> dict | None:
    """One history sample; each window parsed INDEPENDENTLY.

    The proxy may write any non-empty header subset, so a valid 5h-only or
    7d-only observation persists its window rather than vanishing. Non-finite
    values invalidate only their own window. No valid window -> no sample.
    Bounds are validated on the CANONICAL (persisted) values, so the writer
    never acknowledges a row its own reader would refuse.
    """
    headers = state.get("headers") or {}
    ts = _observation_ts(state)
    if ts is None or not (0 < ts <= now + MAX_FUTURE_SKEW_S):
        return None
    sample: dict = {"ts": ts}
    for w, (uk, rk) in _WINDOW_KEYS.items():
        u = _finite(headers.get(_HDR.format(w=w, f="utilization")))
        r = _finite(headers.get(_HDR.format(w=w, f="reset")))
        if u is None or u < 0 or r is None or r != int(r):
            continue  # a fractional reset cannot canonicalize losslessly
        ri = int(r)
        if not (ri - WINDOW_SPANS[w] <= ts <= ri):
            continue  # an observation outside its own window is inconsistent
        sample[uk], sample[rk] = u, ri
    if len(sample) == 1:
        return None
    return sample if _valid_row(sample, now) == sample else None


def _valid_row(rec: dict, now: float) -> dict | None:
    """Canonical form of one stored row, or None if unusable.

    A row needs a finite ts no further than the skew bound past `now`, and at
    least one internally consistent window (finite utilization, losslessly
    integer reset, ts inside [reset-span, reset]); an inconsistent window is
    stripped rather than sinking the row. Window checks alone cannot refuse a
    correlated absurdity like ts=reset=1e100 — the caller's clock does.
    """
    if not isinstance(rec, dict):
        return None
    ts = _finite(rec.get("ts"))
    if ts is None or not (0 < ts <= now + MAX_FUTURE_SKEW_S):
        return None
    if rec.get("tomb") is True:
        # All-invalid observation: no chart data, but it advances the order
        # guard, so a delayed older sample can never slip in behind it.
        return {"ts": ts, "tomb": True}
    out = {"ts": ts}
    for w, (uk, rk) in _WINDOW_KEYS.items():
        u, r = _finite(rec.get(uk)), _finite(rec.get(rk))
        # Domain bound u >= 0; no upper bound needed — every consumer
        # clamps (Y() at 1.2, projected_end at 2.0), keeping output finite.
        if u is None or u < 0 or r is None or r != int(r):
            continue
        ri = int(r)
        if not (ri - WINDOW_SPANS[w] <= ts <= ri):
            continue
        out[uk], out[rk] = u, ri
    return out if len(out) > 1 else None


def _canonical_history(history_path: Path, now: float) -> "tuple[list[dict], bool, bool]":
    """Validated rows in strictly increasing ts, plus a physical-dirty flag.

    dirty=True whenever the file holds anything the canonical view dropped —
    unparseable lines, invalid rows, stripped windows, or order violations —
    so the writer knows a compaction rewrite is owed.
    """
    try:
        lines = history_path.read_text().splitlines()
    except FileNotFoundError:
        return [], False, True   # genuinely empty
    except OSError:
        # An EXISTING but unreadable file is not an empty history; saying so
        # would let a transient error fork the record.
        return [], False, False
    rows: list[dict] = []
    dirty = False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            dirty = True
            continue
        row = _valid_row(rec, now)
        if row is None:
            dirty = True
            continue
        if isinstance(rec, dict) and set(rec) != set(row):
            dirty = True  # a window was stripped
        if rows and row["ts"] <= rows[-1]["ts"]:
            dirty = True  # out-of-order physical line never joins the canon
            continue
        rows.append(row)
    return rows, dirty, True


def _read_history(history_path: Path, now: float | None = None) -> list[dict]:
    """Chart-facing view: the canonical rows only."""
    import time
    return _canonical_history(history_path, time.time() if now is None else now)[0]


def _latest_path_cleanup(history_path: Path) -> None:
    """Best-effort removal of the retired .latest.json sidecar.

    History (with tombstone rows) is the single record; a stale sidecar on
    disk is inert but confusing, so sweep it when convenient."""
    try:
        history_path.with_name(history_path.name + ".latest.json").unlink()
    except OSError:
        pass


def record_sample(state: dict, history_path: Path, now: float | None = None) -> bool:
    """Append one sample if it says something new; True when durably appended.

    Contract (reviewer-driven): the writer owns the physical cap on EVERY
    path, atomicity is cross-process (flock on a sibling), success is durable
    (fsync before the ack), an unreadable existing history refuses the write,
    and the canonical history (tombstone rows included) is the ONE record:
    "current" derives from its tail, so there is no second store to fall
    out of sync with.
    """
    import time
    clock = time.time() if now is None else now
    ts = _observation_ts(state)
    ts_ok = ts is not None and (0 < ts <= clock + MAX_FUTURE_SKEW_S)
    sample = _sample_from_state(state, clock) if ts_ok else None

    def _cap(rows):
        if len(rows) <= MAX_LINES:
            return rows
        return rows[len(rows) - MAX_LINES:]

    def _commit(rows):
        # Only the tail tombstone is the high-water mark; strip superseded
        # ones before the cap so markers never starve data rows of slots.
        rows = [r for i, r in enumerate(rows)
                if not r.get("tomb") or i == len(rows) - 1]
        rows = _cap(rows)
        fd, tmp = tempfile.mkstemp(dir=str(history_path.parent))
        with os.fdopen(fd, "w") as f:
            f.write("".join(json.dumps(r, allow_nan=False) + "\n" for r in rows))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, history_path)

    def _same(rec):
        keys = [k for pair in _WINDOW_KEYS.values() for k in pair]
        return all(rec.get(k) == sample.get(k) for k in keys)

    lock_path = history_path.with_name(history_path.name + ".lock")
    with _LOCK:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                history, dirty, readable = _canonical_history(history_path, clock)
                if not readable:
                    return False  # never fork an unreadable record
                def _refuse():
                    # The cap and compaction are owed on every path, including
                    # the ones that acknowledge nothing.
                    if dirty or len(history) > MAX_LINES:
                        _commit(history)
                    return False

                if not ts_ok:
                    # No usable observation time: neither record moves, but
                    # the cap and compaction stay owed on the existing file.
                    return _refuse()
                # One record: the canonical history tail is the sole
                # ordering authority (tombstone rows included).
                _latest_path_cleanup(history_path)
                if sample is None:
                    tail = float(history[-1]["ts"]) if history else None
                    if tail is not None and ts <= tail:
                        return _refuse()
                    try:
                        _commit(history + [{"ts": ts, "tomb": True}])
                    except OSError:
                        return False
                    return False
                if history:
                    last = history[-1]
                    if float(sample["ts"]) <= float(last["ts"]):
                        return _refuse()
                    if _same(last):
                        if len(history) >= 2 and _same(history[-2]):
                            history[-1] = {**last, "ts": sample["ts"]}
                            try:
                                _commit(history)
                            except OSError:
                                return False
                            return False
                        # Only the run's start exists — append its end below.
                try:
                    if dirty or len(history) + 1 > MAX_LINES:
                        _commit(history + [sample])
                    else:
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
                            f.flush()
                            os.fsync(f.fileno())
                except OSError:
                    return False
                return True
            finally:
                fcntl.flock(lockf, fcntl.LOCK_UN)


def _window_segments(history: list[dict], u_key: str, r_key: str,
                     span: float, now: float, max_windows: int,
                     live_reset: "int | None") -> dict:
    """Normalize samples into per-reset segments of equal width.

    Segment k (0-based, oldest first) holds points (x, y): x = fraction of
    that window elapsed at sample time, y = utilization. The even-pace line
    inside every segment is y = x, so above it is over-use, below under-use.
    """
    by_reset: dict[int, list[dict]] = {}
    newest_ts: dict[int, float] = {}
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
        newest_ts[reset] = max(newest_ts.get(reset, ts), ts)
    # Truncation keys on observation recency, never reset magnitude (resets
    # can shrink); the latest observation's window survives unconditionally.
    chosen = sorted(by_reset, key=lambda r: newest_ts[r])[-max_windows:]
    if live_reset is not None and live_reset in by_reset and live_reset not in chosen:
        chosen[0] = live_reset
        chosen.sort(key=lambda r: newest_ts[r])
    segments = []
    for reset in chosen:
        pts = sorted(by_reset[reset], key=lambda p: p["x"])
        # Current = the latest observation's window, and only while it is
        # open: an expired reset is history, never current or projected.
        current = (live_reset is not None and reset == live_reset
                   and now <= reset)
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
    history = _read_history(history_path, now)
    tail = history[-1] if history else None

    def _live_reset(w):
        # The tail row IS the latest observation; a tomb row or a stripped
        # window means it was invalid then — nothing current until newer.
        if tail is None or tail.get("tomb"):
            return None
        r = tail.get(_WINDOW_KEYS[w][1])
        return int(r) if r is not None else None

    return {
        "now": now,
        "windows": {
            "5h": _window_segments(history, "u5", "r5", WINDOW_SPANS["5h"], now,
                                   max_windows, _live_reset("5h")),
            "7d": _window_segments(history, "u7", "r7", WINDOW_SPANS["7d"], now,
                                   max_windows, _live_reset("7d")),
        },
    }

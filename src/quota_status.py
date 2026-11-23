#!/usr/bin/env python3
"""The core's Claude quota as a room-visible status (`quota_status.v1`): one projection, one debounce.

`project()` turns the quota record (`state/quota-state.json`) into the small block the gateway
bridge sends as `health.quota` on its heartbeat, for the s.space agent sidebar. `read()` projects
only a record of the core's own login, by the rule the recovery record and the limit hold use
(`quota_availability.record_for_account`, which holds a status line record to `trusted_record`
and a proxy record to its `acct_` label). The block carries only the 5-hour and
7-day used percentages and reset times, the limit state, when a limit resumes, and when the
record was last checked. Never the account digest, the `accounts` map, headers, the rejection
ledger, the model or anything else in the record.

`Debounced` holds the block steady between meaningful changes, so the heartbeat's own
change check beats early only when a percentage moved by at least one point or the limit state
flipped; otherwise the held block is refreshed at most every five minutes.

    quota_status.py show     the block as it would be sent now (read-only)

The literal wire example is `contracts/quota_status.v1/heartbeat.json`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quota_availability as qa
from workspace_default import resolve_workspace, status_read_path

CAPABILITY = "quota_status.v1"
VERSION = 1
LIMIT_STATES = ("allowed", "overage", "rejected")
WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
MIN_POINTS = 1.0
MAX_HOLD_S = 300.0


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: Any) -> Optional[int]:
    n = _num(value)
    if n is not None:
        return int(n)
    at = qa._parse_when(value)
    return int(at) if at is not None else None


def _num_str(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window(record: dict, short: str) -> Optional[dict]:
    headers = record.get("headers") if isinstance(record.get("headers"), dict) else {}
    frac = _num(record.get(f"utilization_{short}"))
    if frac is None:
        try:
            frac = float(headers.get(f"anthropic-ratelimit-unified-{short}-utilization"))
        except (TypeError, ValueError):
            return None
    reset = _epoch(record.get(f"resets_at_{short}"))
    if reset is None:
        reset = _epoch(_num_str(headers.get(f"anthropic-ratelimit-unified-{short}-reset")))
    return {"used_pct": round(frac * 100.0, 1), "resets_at": reset}


def _limit_state(record: dict) -> str:
    state = record.get("limit_state")
    return state if state in LIMIT_STATES else qa.classify_limit_state(record)


def _resumes_at(record: dict, windows: dict, now: float) -> Optional[int]:
    until = _epoch(record.get("rejected_until"))
    if until is not None and until > now:
        return until
    ahead = [(w["used_pct"], w["resets_at"]) for w in windows.values()
             if w.get("resets_at") is not None and w["resets_at"] > now]
    return max(ahead)[1] if ahead else None


def project(record: Any, now: Optional[float] = None) -> Optional[dict]:
    """The `quota_status.v1` block for a trusted record, or None when it names no window."""
    if not isinstance(record, dict):
        return None
    at = time.time() if now is None else now
    windows = {}
    for name, short in WINDOWS:
        win = _window(record, short)
        if win is not None:
            windows[name] = win
    if not windows:
        return None
    state = _limit_state(record)
    block = {"v": VERSION, **windows, "limit_state": state}
    if state != "allowed":
        block["resumes_at"] = _resumes_at(record, windows, at)
    block["last_checked"] = _epoch(record.get("last_checked"))
    return block


def read(workspace: Path, runtime: Optional[str] = None, now: Optional[float] = None) -> Optional[dict]:
    """The block for the core's current login, or None (a Codex core has no Claude quota)."""
    if runtime == "codex":
        return None
    try:
        raw = json.loads(status_read_path("quota-state.json", Path(workspace)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # record_for_account applies trusted_record to a status line record; a proxy record needs its label.
    return project(qa.record_for_account(raw, _core_account(workspace)), now)


def _core_account(workspace: Path) -> Optional[str]:
    """The digest of the account the core is signed in to, as the recovery record reads it."""
    try:
        import core_limit_hold
        return core_limit_hold.launch_identity(Path(workspace))[1]
    except Exception:  # noqa: BLE001 -- an unreadable identity is an unknown account
        return None


def moved(old: Optional[dict], new: Optional[dict], min_points: float = MIN_POINTS) -> bool:
    """Whether *new* differs from *old* enough to publish before the hold expires."""
    if (old is None) != (new is None):
        return True
    if old is None or new is None:
        return False
    if old.get("limit_state") != new.get("limit_state"):
        return True
    for name, _short in WINDOWS:
        a, b = old.get(name), new.get(name)
        if (a is None) != (b is None):
            return True
        if a and b and abs(a["used_pct"] - b["used_pct"]) >= min_points:
            return True
    return False


class Debounced:
    """A reader whose value changes only on `moved()` or after `max_hold_s`. Never raises."""

    def __init__(self, reader: Callable[[], Optional[dict]], *, min_points: float = MIN_POINTS,
                 max_hold_s: float = MAX_HOLD_S, clock: Callable[[], float] = time.time):
        self._reader, self._min, self._max, self._clock = reader, min_points, max_hold_s, clock
        self._held: Optional[dict] = None
        self._held_at: Optional[float] = None

    def __call__(self) -> Optional[dict]:
        now = self._clock()
        try:
            new = self._reader()
        except Exception:  # noqa: BLE001 -- a failed read keeps the last published block
            return self._held
        new = new if isinstance(new, dict) else None
        if (self._held_at is None or moved(self._held, new, self._min)
                or now - self._held_at >= self._max):
            self._held, self._held_at = new, now
        return self._held


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["show"]:
        print("usage: quota_status.py show", file=sys.stderr)
        return 2
    print(json.dumps(read(Path(resolve_workspace(migrate=False))), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""One writer of `state/quota-state.json` from Claude Code's own reports, for logins
without the credential proxy: the status line's `rate_limits` and the StopFailure
hook's `error: "rate_limit"`. The record has the proxy's shape (`quotaStateFromHeaders`,
pinned by tests/fixtures/quota-record-shape.parity.json), headers synthesized.

Contract: atomic replace; an observation older than `last_checked` is dropped;
`recent_rejections` and `credential_state*` carry across; no usable `rate_limits`,
no write; the lock is waited on briefly, then the observation is skipped.

Per account: each write is keyed by the writing session's login (`claude_account`). The
top level is the core's account only, so readers see the core's numbers; every account's
latest entry is under `accounts`. Another login never changes the core's numbers or status.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

SOURCE = "statusline"
QUOTA_FILE = "quota-state.json"
MAX_RECENT_REJECTIONS = 20  # the proxy's MAX_RECENT_REJECTIONS
P = "anthropic-ratelimit-unified-"
WINDOWS = (("five_hour", "5h"), ("seven_day", "7d"))
LOCK_WAIT_S = 0.5


def iso(epoch: float) -> str:
    """The proxy's `Date.toISOString()` shape: UTC, milliseconds, `Z`."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def windows_from_statusline(status: Any) -> Optional[dict]:
    """{"5h": (used 0-100, resets_at epoch), ...} for each window reported whole;
    None when `rate_limits` is absent, empty, or carries a window missing a field."""
    limits = status.get("rate_limits") if isinstance(status, dict) else None
    if not isinstance(limits, dict):
        return None
    out = {}
    for key, short in WINDOWS:
        if key not in limits or limits[key] is None:
            continue
        win = limits[key]
        used = _number(win.get("used_percentage")) if isinstance(win, dict) else None
        reset = _number(win.get("resets_at")) if isinstance(win, dict) else None
        if used is None or reset is None:
            return None
        out[short] = (used, reset)
    return out or None


def _fraction(used: float) -> str:
    return format(round(used / 100.0, 6), "g")


def synth_headers(windows: dict, status: str) -> dict:
    headers = {f"{P}status": status}
    for short, (used, reset) in windows.items():
        headers[f"{P}{short}-utilization"] = _fraction(used)
        headers[f"{P}{short}-reset"] = str(int(reset))
    return headers


def _ledger(prev: dict) -> list:
    led = prev.get("recent_rejections")
    return [r for r in led if isinstance(r, dict) and isinstance(r.get("ts"), str)
            and isinstance(r.get("status"), (int, float)) and not isinstance(r.get("status"), bool)
            ] if isinstance(led, list) else []


def _carry(prev: dict, state: dict) -> None:
    if isinstance(prev.get("credential_state"), str):
        for k in ("credential_state", "credential_state_detail", "credential_state_at"):
            state[k] = prev.get(k)


def _model(status: Any) -> str:
    m = status.get("model") if isinstance(status, dict) else None
    mid = m.get("id") if isinstance(m, dict) else m
    return mid if isinstance(mid, str) else ""


def _rejection_holds(prev: dict, windows: dict, now: float) -> bool:
    """A StopFailure rejection stands until its window's reset passes, or a reply
    shows every window under its limit with numbers that moved since the 429."""
    if prev.get("rejected_by") != "stopfailure":
        return False
    until = _number(prev.get("rejected_until"))
    if until is not None and now >= until:
        return False
    old = prev.get("headers") if isinstance(prev.get("headers"), dict) else {}
    moved = any(old.get(f"{P}{s}-utilization") != _fraction(u) or old.get(f"{P}{s}-reset") != str(int(r))
                for s, (u, r) in windows.items())
    return not (moved and all(u < 100 for u, _r in windows.values()))


def record_from_statusline(prev: Any, status: Any, observed_at: float,
                           now: Optional[float] = None) -> Optional[dict]:
    """The record one status line observation writes, or None for no write (no
    usable `rate_limits`, or older than the record on disk)."""
    prev = prev if isinstance(prev, dict) else {}
    windows = windows_from_statusline(status)
    if windows is None:
        return None
    checked = _parse_iso(prev.get("last_checked"))
    if checked is not None and observed_at < checked:
        return None
    now = time.time() if now is None else now
    held = _rejection_holds(prev, windows, now)
    at = iso(observed_at)
    state: dict = {
        "available": not held,
        "last_checked": at,
        "headers": synth_headers(windows, "rejected" if held else "allowed"),
        "recent_rejections": _ledger(prev),
    }
    model = _model(status)
    if model:
        state["last_request"] = {"model": model, "at": at}
    elif isinstance(prev.get("last_request"), dict):
        state["last_request"] = prev["last_request"]
    _carry(prev, state)
    for short, (used, reset) in windows.items():
        state[f"utilization_{short}"] = float(_fraction(used))
        state[f"resets_at_{short}"] = iso(reset)
    state["limit_state"] = "rejected" if held else "allowed"
    if held:
        for k in ("exhausted_since", "rejected_by", "rejected_until"):
            if k in prev:
                state[k] = prev[k]
    state["source"] = SOURCE
    return state


def record_with_rate_limit(prev: Any, at: float, detail: str = "") -> dict:
    """The record after a StopFailure `rate_limit`: a 429 in the ledger, and the
    record rejected until the reset of its most-used window still ahead."""
    prev = prev if isinstance(prev, dict) else {}
    headers = dict(prev["headers"]) if isinstance(prev.get("headers"), dict) else {}
    headers[f"{P}status"] = "rejected"
    best = None
    for short in ("5h", "7d"):
        try:
            u = float(headers.get(f"{P}{short}-utilization"))
            r = float(headers.get(f"{P}{short}-reset"))
        except (TypeError, ValueError):
            continue
        if r > at and (best is None or (u, r) > best):
            best = (u, r)
    rej = {"ts": iso(at), "status": 429, "source": "stopfailure", "snippet": detail[:2048]}
    last = prev.get("last_request")
    if isinstance(last, dict) and isinstance(last.get("model"), str):
        rej["model"] = last["model"]
    ledger = (_ledger(prev) + [rej])[-MAX_RECENT_REJECTIONS:]
    state = {**prev, "headers": headers, "recent_rejections": ledger, "available": False,
             "limit_state": "rejected", "exhausted_since": iso(at), "rejected_by": "stopfailure"}
    state.pop("rejected_until", None)
    if best is not None:
        state["rejected_until"] = best[1]
    if not prev:
        state["source"] = SOURCE
    return state


def observed_at(status: Any, now: Optional[float] = None) -> float:
    """When the numbers were last current. The status line re-renders on a timer
    with the last numbers it has; its transcript moves only with the session."""
    now = time.time() if now is None else now
    path = status.get("transcript_path") if isinstance(status, dict) else None
    if isinstance(path, str) and path:
        try:
            return min(now, os.stat(path).st_mtime)
        except OSError:
            pass
    return now


def quota_path(workspace=None) -> Path:
    from workspace_default import resolve_workspace, status_path
    return status_path(QUOTA_FILE, Path(workspace) if workspace is not None else resolve_workspace())


def read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_atomic(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _update(workspace, build, wait: float) -> bool:
    from file_lock import try_lock_fd, unlock_fd
    path = quota_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + wait
        while not try_lock_fd(fd):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        try:
            state = build(read(path))
            if state is None:
                return False
            write_atomic(path, state)
            return True
        finally:
            unlock_fd(fd)
    finally:
        os.close(fd)


MAX_ACCOUNTS = 8
_OWN_KEYS = ("accounts", "account")


def _entry(rec: Any) -> dict:
    return {k: v for k, v in rec.items() if k not in _OWN_KEYS} if isinstance(rec, dict) else {}


def route(prev: Any, key: str, is_core: bool, build) -> Optional[dict]:
    """Apply `build` to this account's entry. The top level stays the core's account, so
    existing readers see only the core; every account's latest is kept under `accounts`."""
    prev = prev if isinstance(prev, dict) else {}
    accounts = {k: v for k, v in (prev.get("accounts") or {}).items() if isinstance(v, dict)} \
        if isinstance(prev.get("accounts"), dict) else {}
    entry = build(_entry(prev) if is_core else accounts.get(key, {}))
    if entry is None:
        return None
    accounts[key] = entry
    while len(accounts) > MAX_ACCOUNTS:
        oldest = min((k for k in accounts if k != key), key=lambda k: str(accounts[k].get("last_checked") or ""))
        del accounts[oldest]
    out = {**entry, "account": key} if is_core else dict(prev)
    out["accounts"] = accounts
    return out


def _who(config_dir: Optional[str], core_dir: Optional[str]) -> tuple:
    from claude_account import account_key, core_config_dir, same_login
    config_dir = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or None
    core_dir = core_dir if core_dir is not None else core_config_dir()
    return account_key(config_dir), same_login(config_dir, core_dir)


def record_statusline(workspace, status: Any, now: Optional[float] = None,
                      config_dir: Optional[str] = None, core_dir: Optional[str] = None) -> bool:
    """Record one status line observation under the session's account; True when written.
    `config_dir` defaults to the session's CLAUDE_CONFIG_DIR, `core_dir` to the core's."""
    if windows_from_statusline(status) is None:
        return False
    seen = observed_at(status, now)
    key, is_core = _who(config_dir, core_dir)
    return _update(workspace, lambda prev: route(
        prev, key, is_core, lambda e: record_from_statusline(e, status, seen, now)), LOCK_WAIT_S)


def record_rate_limit(workspace, now: Optional[float] = None, detail: str = "",
                      config_dir: Optional[str] = None, core_dir: Optional[str] = None) -> bool:
    """Record one StopFailure `rate_limit` under the session's account; True when written."""
    at = time.time() if now is None else now
    key, is_core = _who(config_dir, core_dir)
    return _update(workspace, lambda prev: route(
        prev, key, is_core, lambda e: record_with_rate_limit(e, at, detail)), 2.0)

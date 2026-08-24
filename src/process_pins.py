"""Process-side restart pins: which running pids must NOT be restarted, and why.

`_checkout_is_canonical` asks whether the CHECKOUT is safe to restart a bridge
FROM. It cannot describe what a running process already imported. Python binds
module code at import, so a process launched from a branch keeps that branch
after the tree returns to main — and the stale probe, which compares source
mtime against process start, reports the identical `restart needed` for a tree
that moved FORWARD (restart adopts newer code, correct) and one that moved
BACKWARD (restart discards code that only exists in that process).

A pin names the second case. It is deliberately hard to trust:

  - identity is (pid, lstart), never a bare pid — pids are reused, and a bare
    one lets an unrelated successor inherit the suppression silently
  - every pin carries `expires_at`; suppression that cannot expire fails
    silent, which is worse than the warning it replaced
  - a pin that no longer matches a live process is a FINDING, not silence:
    the pinned process died and whatever it was protecting is already gone

The caller supplies the pin file's path — this module names no location, so the
adapter that already resolves the workspace stays the one that decides. Shape:

    {"pins": [{"service": "discord-bridge", "pid": 87258,
               "lstart": "Sat Aug 23 12:24:57 2026",
               "reason": "#2604 witness armed - restart re-imports main",
               "expires_at": "2026-08-31T00:00:00Z"}]}
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ARMED = "armed"
EXPIRED = "expired"
MISMATCH = "mismatch"
ORPHAN = "orphan"


def load_pins(path) -> list:
    """Pins for `path`, or [] when absent/unreadable.

    Unreadable fails OPEN on purpose: a pin only ever SUPPRESSES a restart
    prescription, so a broken file must not be able to suppress anything.
    """
    try:
        data = json.loads(Path(path).read_text())
    except Exception:  # noqa: BLE001 — see docstring: absent/broken -> no pins
        return []
    pins = data.get("pins") if isinstance(data, dict) else None
    return [p for p in pins if isinstance(p, dict)] if isinstance(pins, list) else []


def _expired(pin: dict, now_ts: float) -> bool:
    raw = str(pin.get("expires_at") or "").strip()
    if not raw:
        return True          # no expiry declared -> treat as expired, never eternal
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() <= now_ts


def evaluate(pins: list, service: str, lstart_by_pid: dict, now_ts: float) -> list:
    """[(verdict, pin, detail)] for every pin naming `service`.

    `lstart_by_pid` maps the service's CURRENTLY RUNNING pids (as str) to the
    `ps -o lstart=` string. A pin is ARMED only if its pid is live AND its
    recorded lstart still matches — every other outcome is reported so a lost
    pin surfaces instead of quietly suppressing or quietly disappearing.
    """
    out = []
    for pin in pins:
        if str(pin.get("service") or "") != service:
            continue
        pid = str(pin.get("pid") or "")
        reason = str(pin.get("reason") or "no reason recorded")
        live = lstart_by_pid.get(pid)
        if live is None:
            out.append((ORPHAN, pin, (
                f"pin names {service} pid {pid}, which is no longer running — "
                f"whatever it protected ({reason}) is already gone; remove the pin")))
        elif str(pin.get("lstart") or "").strip() != str(live).strip():
            out.append((MISMATCH, pin, (
                f"pin names {service} pid {pid} but that pid now belongs to a "
                f"process started {live} — the pinned process is gone; remove the pin")))
        elif _expired(pin, now_ts):
            out.append((EXPIRED, pin, (
                f"pin on {service} pid {pid} expired ({pin.get('expires_at') or 'no expiry declared'}) "
                f"— re-pin deliberately or restart; original reason: {reason}")))
        else:
            out.append((ARMED, pin, (
                f"DO NOT RESTART {service} pid {pid} — {reason} "
                f"(pin expires {pin.get('expires_at')})")))
    return out


def armed_detail(results: list):
    """The first ARMED detail, or None. A caller uses this to REPLACE a
    `restart needed` prescription; non-armed results must still be surfaced."""
    for verdict, _pin, detail in results:
        if verdict == ARMED:
            return detail
    return None

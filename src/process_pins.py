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

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ARMED = "armed"
EXPIRED = "expired"
MISMATCH = "mismatch"
ORPHAN = "orphan"
PROBE_FAILED = "probe-failed"

# Writer contract: schema = _REQUIRED string fields; bounds = MAX_PINS /
# _FIELD_MAX; atomicity = temp + os.replace; writers raise, only the READER fails open.
MAX_PINS = 32
_FIELD_MAX = 500
_REQUIRED = ("service", "pid", "lstart", "reason", "expires_at")


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


def _validated(pin) -> dict:
    """One pin, normalized to the documented shape. Raises ValueError."""
    if not isinstance(pin, dict):
        raise ValueError(f"pin must be a dict, got {type(pin).__name__}")
    out = {}
    for key in _REQUIRED:
        val = str(pin.get(key) or "").strip()
        if not val:
            raise ValueError(f"pin missing required field {key!r}")
        if len(val) > _FIELD_MAX:
            raise ValueError(f"pin field {key!r} exceeds {_FIELD_MAX} chars")
        out[key] = val
    out["pid"] = str(int(out["pid"]))
    dt = datetime.fromisoformat(out["expires_at"].replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("expires_at must carry a timezone")
    return out


def save_pins(path, pins: list) -> None:
    """Validated, bounded, ATOMIC snapshot write.

    Same-directory temp + os.replace, so a concurrent load_pins() observes
    only the complete old or complete new snapshot — never a truncated
    intermediate, which the fail-open reader would translate into "no pins"
    and hand the restart prescription back the veto it was suppressing.
    """
    if len(pins) > MAX_PINS:
        raise ValueError(f"{len(pins)} pins exceeds the bound of {MAX_PINS}")
    payload = json.dumps({"pins": [_validated(p) for p in pins]}, indent=1)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    try:
        tmp.write_text(payload)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _load_strict(path) -> list:
    """Writer-side load: absent file is [], but malformed state RAISES.
    Only the reader may fail open — a writer that fails open destroys pins."""
    target = Path(path)
    if not target.exists():
        return []
    data = json.loads(target.read_text())
    pins = data.get("pins") if isinstance(data, dict) else None
    if not isinstance(pins, list):
        raise ValueError(f"{target}: existing pin record is malformed")
    # Every entry through the production schema: dropping a bad one would
    # rewrite the record around it — the reader's fail-open, not a writer's.
    return [_validated(p) for p in pins]


@contextmanager
def _locked(path):
    """Serialize the whole read-modify-write: os.replace keeps SNAPSHOTS whole,
    but only a lock keeps two concurrent writers from dropping each other."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f".{target.name}.lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def arm_pin(path, service, pid, lstart, reason, expires_at) -> dict:
    """The supported arm/extend entry point. Replaces any pin with the same
    (service, pid) identity, keeps the rest, writes through save_pins()."""
    pin = _validated({"service": service, "pid": pid, "lstart": lstart,
                      "reason": reason, "expires_at": expires_at})
    with _locked(path):
        keep = [p for p in _load_strict(path)
                if not (str(p.get("service") or "") == pin["service"]
                        and str(p.get("pid") or "") == pin["pid"])]
        save_pins(path, keep + [pin])
    return pin


def release_pin(path, service, pid=None, lstart=None) -> int:
    """The supported release entry point. Returns how many pins it removed.

    Identity is (pid, lstart), never a bare pid: after PID reuse a stale
    cleanup carrying only the number would delete the NEW process's pin.
    A service-wide release stays available, but only as an explicit choice
    (pid=None) — never as the accidental result of an unmatched pid.
    """
    if pid is not None and not str(lstart or "").strip():
        raise ValueError(
            "release_pin: a pid-targeted release requires lstart — a bare pid "
            "cannot tell a reused pid's new process from the pinned one")
    with _locked(path):
        pins = _load_strict(path)

        def _targeted(p) -> bool:
            if str(p.get("service") or "") != str(service):
                return False
            if pid is None:
                return True
            return (str(p.get("pid") or "") == str(pid)
                    and str(p.get("lstart") or "").strip() == str(lstart).strip())

        keep = [p for p in pins if not _targeted(p)]
        if len(keep) != len(pins):
            save_pins(path, keep)
    return len(pins) - len(keep)


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

    `lstart_by_pid=None` means the ENUMERATION FAILED: unknown is not the empty
    set, so no pin may be called ORPHAN and no restart may lean on the result.
    """
    out = []
    for pin in pins:
        if str(pin.get("service") or "") != service:
            continue
        pid = str(pin.get("pid") or "")
        reason = str(pin.get("reason") or "no reason recorded")
        if lstart_by_pid is None:
            # Expiry outranks the unknown: a pin past (or without) its expiry
            # must never regain the veto through a failing probe (eternal suppression).
            if _expired(pin, now_ts):
                out.append((EXPIRED, pin, (
                    f"pin on {service} pid {pid} expired ({pin.get('expires_at') or 'no expiry declared'}) "
                    f"— re-pin deliberately or restart; original reason: {reason}")))
            else:
                out.append((PROBE_FAILED, pin, (
                    f"pin on {service} pid {pid} could not be verified — process "
                    f"enumeration failed; not orphaned, and no restart may be "
                    f"authorized on an unknown ({reason})")))
            continue
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


def other_notes(results: list) -> str:
    """Non-ARMED pin notes, formatted. An armed sibling changes the
    prescription; it does not retract the other pins' findings."""
    return "".join(f" [{note}]" for verdict, _pin, note in results
                   if verdict != ARMED)


def verdict_for(results: list, warn_lead: str, stale_detail: str) -> tuple:
    """(status, detail) for ANY prescription an armed pin must override.

    Every restart/rebuild prescription routes through here, not just the
    src-vs-process one: a pin preserves a branch-only compiled witness, and a
    rebuild destroys it exactly as a restart does.
    """
    veto = veto_detail(results)
    # The veto's own note must not repeat in the trailing findings.
    others = other_notes([r for r in results if r[2] != veto])
    if veto:
        return ("warn", f"{warn_lead}, but {veto}{others}")
    return ("stale", f"{stale_detail}{others}")


def stale_verdict(results: list, age_min: int) -> tuple:
    """(status, detail) for a process the mtime check has already called stale."""
    return verdict_for(
        results,
        f"code is {age_min} min newer than process",
        f"running but code is {age_min} min newer than process "
        f"\u2014 restart needed")


def veto_detail(results: list):
    """The first detail that forbids a restart: ARMED, or PROBE_FAILED —
    an unverifiable pin must not be destroyed on the strength of a failed probe."""
    for verdict, _pin, detail in results:
        if verdict in (ARMED, PROBE_FAILED):
            return detail
    return None


def armed_detail(results: list):
    """The first ARMED detail, or None. A caller uses this to REPLACE a
    `restart needed` prescription; non-armed results must still be surfaced."""
    for verdict, _pin, detail in results:
        if verdict == ARMED:
            return detail
    return None

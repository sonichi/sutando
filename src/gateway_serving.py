"""Shared owner for the `gateway-status.json` sidecar verdict.

Three readers interpret this record — `health-check._gateway_serving`,
`core-input-watch._gateway_status` and `services_status.probe_gateway`. The
freshness window, the reconnect grace and the rendering differ per reader and
stay at the edges; what lives here is the part they must agree on: whether a
record is fresh enough to have an opinion, and whether it says the lane is
actually serving.

`connected` is a flag someone SET; `last_ok_ts` is the value that ADVANCES. A
lane that has never completed a poll carries `connected: true` with
`last_ok_ts: null`, and a reader trusting the flag alone calls that healthy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# A record stamped further ahead than this is not clock skew, it is corruption.
FUTURE_SKEW_S = 5.0

# Selector values that mean "every lane", never one lane's file.
_NOT_A_LANE = ("all", "primary")


def active_lane() -> str | None:
    """The desktop's signed-in gateway lane: the first line of
    $SUTANDO_APP_SUPPORT/channels/active-gateway-lane when it is an instance name by the bridge's own
    bound (connections.INSTANCE_RE) other than all/primary. None otherwise, and for a process started
    without that variable (a shell-run job), which therefore reads the primary connection's sidecar."""
    root = (os.environ.get("SUTANDO_APP_SUPPORT") or "").strip()
    if not root:
        return None
    try:
        lines = (Path(root) / "channels" / "active-gateway-lane").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    lane = lines[0].strip() if lines else ""
    from connections import INSTANCE_RE
    return lane if INSTANCE_RE.fullmatch(lane) and lane not in _NOT_A_LANE else None


def status_path(state_dir) -> Path:
    """The active lane's `gateway-status.<lane>.json` when it exists, else the primary connection's
    sidecar (the unsuffixed file without a primary row). s.space signs in as a secondary lane."""
    lane = active_lane()
    if lane:
        p = Path(state_dir) / f"gateway-status.{lane}.json"
        try:
            if p.exists():
                return p
        except OSError:
            pass
    from connections import primary_sidecar_name
    return Path(state_dir) / primary_sidecar_name(state_dir)


def safe_num(v, *, nonneg: bool = False) -> float | None:
    """The value as a finite float, or None. `bool` is rejected: it passes
    isinstance(_, int) and would make `True` a valid timestamp."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    # json parses ints of any size; float() raises OverflowError past ~1e308.
    try:
        f = float(v)
    except OverflowError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if nonneg and f < 0:
        return None
    return f


@dataclass(frozen=True)
class GatewayVerdict:
    """A fresh sidecar record, normalized. Only built when the record is fresh —
    absence of an opinion is represented by `None` in place of this object, so a
    verdict never has to encode "I don't know"."""

    ts: float
    connected: bool
    last_ok_ts: float | None
    backoff_s: float | None

    @property
    def serving(self) -> bool:
        """Whether the lane is actually carrying traffic.

        Requires BOTH the flag and evidence of a completed poll. `connected`
        alone is what a dead bridge's last write leaves behind.
        """
        return self.connected and self.last_ok_ts is not None

    @property
    def never_polled(self) -> bool:
        """Claims connection but has no successful poll to point at."""
        return self.connected and self.last_ok_ts is None


def verdict_from_record(data, *, now: float, max_age: float) -> GatewayVerdict | None:
    """Verdict for an already-parsed record, or None for no opinion.

    None when the record is not a mapping, carries no usable `ts`, or is older
    than `max_age` — a stale sidecar means the bridge may be wedged, and the
    caller's process probe answers instead.
    """
    if not isinstance(data, dict):
        return None
    ts = safe_num(data.get("ts"), nonneg=True)
    if ts is None:
        return None
    age = now - ts
    # Two-sided: a future record cannot describe a poll that has happened, and
    # would otherwise read as fresh until the clock caught up.
    if age > max_age or age < -FUTURE_SKEW_S:
        return None
    connected = data.get("connected")
    # A non-bool is schema drift, not a value. Coercing it makes "false" true.
    if not isinstance(connected, bool):
        return None
    last_ok = safe_num(data.get("last_ok_ts"), nonneg=True)
    # A poll cannot have completed in the future; that is not evidence either.
    if last_ok is not None and last_ok - now > FUTURE_SKEW_S:
        last_ok = None
    return GatewayVerdict(
        ts=ts,
        connected=connected,
        last_ok_ts=last_ok,
        backoff_s=safe_num(data.get("backoff_s"), nonneg=True),
    )


def read_verdict(path, *, now: float, max_age: float) -> GatewayVerdict | None:
    """`verdict_from_record` over a path. None when absent/unreadable/malformed."""
    try:
        return verdict_from_record(
            json.loads(Path(path).read_text()), now=now, max_age=max_age
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None

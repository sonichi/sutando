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
from dataclasses import dataclass
from pathlib import Path


def _num(v) -> float | None:
    """The value as a float, or None. `bool` is rejected: it passes
    isinstance(_, int) and would make `True` a valid timestamp."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


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
    ts = _num(data.get("ts"))
    if ts is None or (now - ts) > max_age:
        return None
    return GatewayVerdict(
        ts=ts,
        connected=bool(data.get("connected")),
        last_ok_ts=_num(data.get("last_ok_ts")),
        backoff_s=_num(data.get("backoff_s")),
    )


def read_verdict(path, *, now: float, max_age: float) -> GatewayVerdict | None:
    """`verdict_from_record` over a path. None when absent/unreadable/malformed."""
    try:
        return verdict_from_record(
            json.loads(Path(path).read_text()), now=now, max_age=max_age
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None

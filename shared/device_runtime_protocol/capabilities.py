"""Capability names + the three-set state model.

Owner ruling: `supported` (this runtime implements it) / `available` (system
state currently permits it, with a reason when not) / `granted` (this subject
may call it) are DISTINCT. Conflating them makes "not implemented", "screen
recording permission missing" and "policy says no" indistinguishable.
granted_methods remains only a compatibility source for the granted set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# provider.resource.verb, or provider.verb for providers with one resource.
_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$")


def validate_capability_name(name: str) -> bool:
    return bool(_NAME.match(name or ""))


@dataclass(frozen=True)
class CapabilityState:
    capability: str
    supported: bool
    available: bool
    granted: bool
    reason: str | None = None  # set when supported and not available

    @property
    def callable_now(self) -> bool:
        return self.supported and self.available and self.granted

    def to_dict(self) -> dict:
        d = {
            "capability": self.capability,
            "supported": self.supported,
            "available": self.available,
            "granted": self.granted,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


def resolve_capability_state(
    capability: str,
    *,
    supported: set[str],
    availability: dict[str, str],
    granted: set[str],
) -> CapabilityState:
    """`availability` maps capability -> unavailability reason; absence means
    available. `granted` is the subject's set (e.g. adapted from a device
    credential's granted_methods)."""
    is_supported = capability in supported
    reason = availability.get(capability)
    return CapabilityState(
        capability=capability,
        supported=is_supported,
        available=is_supported and reason is None,
        granted=capability in granted,
        reason=reason if is_supported else None,
    )

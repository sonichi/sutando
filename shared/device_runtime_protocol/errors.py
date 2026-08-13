"""Transport-independent error model (owner ruling: fixed at these ten).

Every fault declares whether a caller may retry AS-IS and whether it must
re-observe first. PRECONDITION_FAILED is generic — page version, foreground
app, window, file hash, git HEAD, target domain, approval digest — and its
default posture is re-observe-and-replan, never blind retry.
"""

from __future__ import annotations

from dataclasses import dataclass

ERROR_CODES = (
    "INVALID_ARGUMENT",
    "CAPABILITY_UNSUPPORTED",
    "CAPABILITY_UNAVAILABLE",
    "PERMISSION_DENIED",
    "APPROVAL_REQUIRED",
    "PRECONDITION_FAILED",
    "CONFLICT",
    "DEADLINE_EXCEEDED",
    "EXECUTION_FAILED",
    "CANCELLED",
)

# code -> (retryable, requires_new_observation) defaults. Producers may
# override retryable when they know better; the observation flag is semantic.
_DEFAULTS = {
    "INVALID_ARGUMENT": (False, False),
    "CAPABILITY_UNSUPPORTED": (False, False),
    "CAPABILITY_UNAVAILABLE": (False, False),
    "PERMISSION_DENIED": (False, False),
    "APPROVAL_REQUIRED": (False, False),
    "PRECONDITION_FAILED": (False, True),
    "CONFLICT": (False, True),
    "DEADLINE_EXCEEDED": (False, False),
    "EXECUTION_FAILED": (True, False),
    "CANCELLED": (False, False),
}


@dataclass(frozen=True)
class ProtocolFault:
    code: str
    message: str
    retryable: bool
    requires_new_observation: bool
    reason: str | None = None

    def to_dict(self) -> dict:
        d = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "requires_new_observation": self.requires_new_observation,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


def fault(code: str, message: str, *, reason: str | None = None,
          retryable: bool | None = None) -> ProtocolFault:
    if code not in _DEFAULTS:
        raise ValueError(f"unknown protocol error code: {code!r}")
    default_retry, needs_obs = _DEFAULTS[code]
    return ProtocolFault(
        code=code,
        message=message,
        retryable=default_retry if retryable is None else retryable,
        requires_new_observation=needs_obs,
        reason=reason,
    )

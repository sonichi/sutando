"""Id minting for the observability + metering spine. Zero dependencies.

Twin of ``ids.ts`` — both languages MUST produce the same format (prefix +
length + alphabet) so an id minted on either side is interchangeable.

  - ``new_trace_id()`` / ``new_usage_id()`` -> ULID-style: a 48-bit millisecond
    timestamp + 80 bits of randomness, Crockford base32, 26 chars, with a
    2-char type prefix (``tr_`` / ``ux_``). Lexicographically TIME-SORTABLE
    (the time component is the high-order chars), URL-safe, collision-resistant.
  - ``new_span_id()`` -> ``sp_`` + 16 hex chars (8 random bytes). Spans need
    uniqueness, not time-sortability.

Crockford base32 omits I, L, O, U to avoid visual ambiguity. The matching
regex is ``^(tr|ux)_[0-9A-HJKMNP-TV-Z]{26}$`` and ``^sp_[0-9a-f]{16}$``.
"""

from __future__ import annotations

import os
import time

__all__ = ["ulid", "new_trace_id", "new_usage_id", "new_span_id"]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_LEN = 10  # 10 base32 chars = 50 bits; holds a 48-bit ms timestamp
_RAND_LEN = 16  # 16 base32 chars = 80 bits of randomness


def _encode_time(ms: int, length: int) -> str:
    out: list[str] = []
    n = int(ms)
    for _ in range(length):
        out.append(_CROCKFORD[n % 32])
        n //= 32
    return "".join(reversed(out))


def _encode_random(length: int) -> str:
    return "".join(_CROCKFORD[b & 31] for b in os.urandom(length))


def ulid(at_ms: int | None = None) -> str:
    """Bare ULID-style body (no prefix). ``at_ms`` overridable for determinism."""
    if at_ms is None:
        at_ms = int(time.time() * 1000)
    return _encode_time(at_ms, _TIME_LEN) + _encode_random(_RAND_LEN)


def new_trace_id(at_ms: int | None = None) -> str:
    return "tr_" + ulid(at_ms)


def new_usage_id(at_ms: int | None = None) -> str:
    return "ux_" + ulid(at_ms)


def new_span_id() -> str:
    return "sp_" + os.urandom(8).hex()

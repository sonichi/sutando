"""Classify an outbound-send failure as transient (retry) or permanent (park).

A bridge that quarantines every failed send is right for a rejection the API will
never accept and wrong for a blip: a 503 becomes a 200 on the next poll, so
parking it strands an owner-facing message that would have delivered on its own.
Unknown failures stay parked — a quarantined file is preserved and surfaced by
health-check, so parking is the safe default and only a KNOWN transient retries.
"""

from __future__ import annotations

# 429 is rate-limiting and 5xx is the server's own fault; both clear on their own.
# 408/425 are request-timing, equally retryable. Every other 4xx is a rejection of
# this specific payload or recipient (413 too large, 403 cannot DM, 400 malformed)
# and will fail identically forever.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Bound the retry so a multi-hour outage parks the message instead of re-polling
# it every 3s until the log is unreadable.
MAX_TRANSIENT_ATTEMPTS = 5

# Connection-level failures carry no HTTP status. Matched by name so this module
# stays import-light — it must not pull in discord.py or aiohttp to be testable.
_TRANSIENT_EXC_NAMES = frozenset({
    "ClientConnectorError",
    "ClientOSError",
    "ClientConnectionError",
    "ClientConnectionResetError",
    "ServerDisconnectedError",
    "ServerTimeoutError",
    "ConnectionClosed",
    "GatewayNotFound",
    "DiscordServerError",
})


def failure_status(exc: BaseException) -> int | None:
    """The HTTP status of a failed send, or None.

    Reads `.status` only. `discord.HTTPException` also carries `.code` — the
    Discord *error* code (40005, 50035) — which shares no numbering with HTTP
    status and would misclassify if treated as one.
    """
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def is_transient(exc: BaseException) -> bool:
    """True when retrying the same send could plausibly succeed."""
    status = failure_status(exc)
    if status is not None:
        return status in TRANSIENT_STATUSES
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def should_retry(exc: BaseException, attempts: int,
                 cap: int = MAX_TRANSIENT_ATTEMPTS) -> bool:
    """True to release the claim for another poll; False to quarantine.

    `attempts` is how many times this same body has already failed transiently.
    """
    return is_transient(exc) and attempts < cap

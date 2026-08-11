"""Classify an outbound-send failure as transient (retry) or permanent (park).

Parking is the safe default: only a KNOWN transient retries.
"""

from __future__ import annotations

from pathlib import Path

# 4xx timing cases only. 5xx is handled as a RANGE below, because enumerating it
# silently parked the Cloudflare statuses (520-527) Discord sits behind.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Bound the retry: a multi-hour outage must park rather than re-poll every 3s.
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

    Reads `.status` only: `.code` is a Discord error code sharing no numbering
    with HTTP status, so treating it as one would misclassify.
    """
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def is_transient(exc: BaseException) -> bool:
    """True when retrying the same send could plausibly succeed.

    The NAME is checked before the status: a status short-circuit made
    `_TRANSIENT_EXC_NAMES` unreachable for any of those types that carries one.
    """
    if type(exc).__name__ in _TRANSIENT_EXC_NAMES:
        return True
    status = failure_status(exc)
    if status is not None:
        # Whole 5xx range: the server failed, not the payload.
        return status in TRANSIENT_STATUSES or 500 <= status <= 599
    return isinstance(exc, (TimeoutError, ConnectionError))


def should_retry(exc: BaseException, attempts: int,
                 cap: int = MAX_TRANSIENT_ATTEMPTS) -> bool:
    """True to release the claim for another poll; False to quarantine.

    `attempts` is how many times this same body has already failed transiently.
    """
    return is_transient(exc) and attempts < cap


def resolve_failed_send(claim: Path, exc: BaseException,
                        attempts: "dict[str, int]", progressed: bool = False) -> str:
    """Decide and CARRY OUT the transition. Returns "retried" (claim released),
    "parked" (moved to undelivered/), or "stuck" (unmovable, left for the next poll).

    `attempts` is mutated in place, keyed by the body's polled `.txt` name.
    """
    key = claim.with_suffix(".txt").name
    tried = attempts.get(key, 0)
    # `progressed` means part of the body already reached the recipient. Re-sending
    # from the start would repeat it, so a partial delivery parks instead.
    if not progressed and should_retry(exc, tried):
        # release_claim refuses to clobber a `.txt` written since the claim, so a
        # False here means a newer body exists — park this one rather than drop it.
        from proactive_recovery import release_claim
        if release_claim(claim):
            attempts[key] = tried + 1
            return "retried"
    attempts.pop(key, None)
    try:
        undelivered = claim.parent / "undelivered"
        undelivered.mkdir(parents=True, exist_ok=True)
        # Drop the `.sending` claim suffix: a quarantined `*.sending` reads as
        # in-flight, which is what the restart sweep looks for.
        claim.rename(undelivered / key)
        return "parked"
    except Exception:
        return "stuck"

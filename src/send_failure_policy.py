"""Classify an outbound-send failure as transient (retry) or permanent (park).

A bridge that quarantines every failed send is right for a rejection the API will
never accept and wrong for a blip: a 503 becomes a 200 on the next poll, so
parking it strands an owner-facing message that would have delivered on its own.
Unknown failures stay parked — a quarantined file is preserved and surfaced by
health-check, so parking is the safe default and only a KNOWN transient retries.
"""

from __future__ import annotations

from pathlib import Path

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


def resolve_failed_send(claim: Path, exc: BaseException,
                        attempts: "dict[str, int]") -> str:
    """Decide and CARRY OUT what happens to a body whose send failed.

    Returns "retried" (claim released, poll will pick it up again), "parked"
    (moved to undelivered/), or "stuck" (could not move it; left in place so the
    poll retries rather than losing it).

    Owns the file transition as well as the decision, because the two cannot
    disagree without stranding a message: a branch that logs "released" without
    renaming leaves the body claimed and invisible to every future poll.
    `attempts` is mutated in place, keyed by the body's polled `.txt` name.
    """
    key = claim.with_suffix(".txt").name
    tried = attempts.get(key, 0)
    if should_retry(exc, tried):
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

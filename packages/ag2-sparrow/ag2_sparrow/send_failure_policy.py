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
    # DNS lookup failure (socket.gaierror — an OSError, not a ConnectionError):
    # a resolver blip retries; the cap parks a genuinely dead hostname.
    "gaierror",
    # Accepted-but-unconfirmed: retryable, but only under the cap (see the class).
    "UnconfirmedDelivery",
})

# Checked BEFORE the transient names: these inherit from a listed transient
# (ClientConnectorError) but a bad cert, wrong CA or pin mismatch is a
# misconfiguration — no number of retries turns it into a 200.
_PERMANENT_EXC_NAMES = frozenset({
    "ClientSSLError",            # + its cert/SSL connector subclasses
    "ServerFingerprintMismatch",  # reaches the set via ServerConnectionError
})


class UnconfirmedDelivery(Exception):
    """The server ACCEPTED a send but did not confirm it (no event id).

    Transient-with-cap on purpose. The send may in fact have been delivered,
    so an UNBOUNDED retry duplicates it — that is the 2026-08-16 incident,
    where one nudge went out 12 times. The cap is what makes retrying safe:
    a momentary withholding still recovers, a systematic one parks loudly.
    """


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

    Names are matched across the whole MRO, not just the concrete class: aiohttp
    raises `ClientConnectorDNSError(ClientConnectorError)` for a DNS failure, and
    an exact-name test misses every such subclass while its listed parent sits
    right above it. `_PERMANENT_EXC_NAMES` is the exception to that widening.
    """
    # urllib's URLError reports the real failure via `.reason`; classify the
    # wrapper by what it wraps, or a wrapped timeout parks on its first attempt.
    for _ in range(4):
        names = [base.__name__ for base in type(exc).__mro__]
        if any(n in _PERMANENT_EXC_NAMES for n in names):
            return False
        if any(n in _TRANSIENT_EXC_NAMES for n in names):
            return True
        status = failure_status(exc)
        if status is not None:
            # Whole 5xx range: the server failed, not the payload.
            return status in TRANSIENT_STATUSES or 500 <= status <= 599
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        reason = getattr(exc, "reason", None)
        if not isinstance(reason, BaseException) or reason is exc:
            return False
        exc = reason
    return False


def should_retry(exc: BaseException, attempts: int,
                 cap: int = MAX_TRANSIENT_ATTEMPTS) -> bool:
    """True to release the claim for another poll; False to quarantine.

    `attempts` is how many times this same body has already failed transiently.
    """
    return is_transient(exc) and attempts < cap


def decide_failed_send(exc: BaseException, tried: int,
                       progressed: bool = False) -> str:
    """The decision alone: "retry" or "park". Executors carry it out.

    `progressed` means part of the body already reached the recipient; a
    re-send from the start would repeat it, so partial delivery always parks.
    """
    return "retry" if (not progressed and should_retry(exc, tried)) else "park"


def resolve_failed_send(claim: Path, exc: BaseException,
                        attempts: "dict[str, int]", progressed: bool = False,
                        *, body: "Path | None" = None,
                        undelivered_dir: "Path | None" = None) -> str:
    """Decide and CARRY OUT the transition. Returns "retried" (claim released),
    "parked" (moved to undelivered/), or "stuck" (unmovable, left for the next poll).

    `attempts` is mutated in place, keyed by the polled body name. `body` and
    `undelivered_dir` let adapters with pid-scoped claim names (which break the
    `.txt`-sibling derivation) bind their own paths — the transition stays here.
    """
    if body is None:
        body = claim.with_suffix(".txt")
    key = body.name
    tried = attempts.get(key, 0)
    if decide_failed_send(exc, tried, progressed) == "retry":
        # release_claim refuses to clobber a `.txt` written since the claim, so a
        # False here means a newer body exists — park this one rather than drop it.
        # Bundled verbatim into ag2_sparrow, where siblings are package
        # submodules; in src/ they are flat modules. Support both.
        try:  # pragma: no cover - exercised by whichever context imports it
            from .proactive_recovery import release_claim
        except ImportError:  # pragma: no cover - flat src/ import path
            from proactive_recovery import release_claim
        if release_claim(claim, target=body):
            attempts[key] = tried + 1
            return "retried"
    attempts.pop(key, None)
    try:
        undelivered = undelivered_dir if undelivered_dir is not None \
            else claim.parent / "undelivered"
        undelivered.mkdir(parents=True, exist_ok=True)
        # Drop the `.sending` claim suffix: a quarantined `*.sending` reads as
        # in-flight, which is what the restart sweep looks for.
        claim.rename(undelivered / key)
        return "parked"
    except Exception:
        return "stuck"

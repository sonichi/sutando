"""Shared Discord REST helper: urlopen with 429 Retry-After + 5xx backoff.

Discord rate-limits (HTTP 429) return a ``Retry-After`` header and a JSON body
``{"retry_after": <seconds>}``. A bare ``urllib.request.urlopen`` raises
``HTTPError`` on a 429, and the caller crashes mid-read — the failure mode that
silently truncated a 30-day channel history read on 2026-07-24 (the reader
scripts had no backoff, so a rate limit ended pagination early with an error).

``request_json`` wraps urlopen, honors ``Retry-After`` (bounded), and applies a
short exponential backoff to transient 5xx responses, so reads survive a rate
limit instead of aborting. It returns the decoded JSON body on success and
re-raises the last error once retries are exhausted.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

# Retry only these codes. 429 = rate limit; 5xx = transient upstream/edge error.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 5
# Cap any single Retry-After/backoff sleep — a pathological Retry-After
# must not wedge a reader for minutes.
MAX_SLEEP_SECONDS = 60.0


def _retry_after_seconds(err: urllib.error.HTTPError, body: object) -> float | None:
    """Extract the retry delay (seconds) from the Retry-After header, then the
    JSON body's ``retry_after``. Return None when neither is parseable."""
    header = err.headers.get("Retry-After") if err.headers else None
    if header:
        try:
            return float(header)
        except (TypeError, ValueError):
            pass
    if isinstance(body, dict) and "retry_after" in body:
        try:
            return float(body["retry_after"])
        except (TypeError, ValueError):
            pass
    return None


def request_json(
    req: urllib.request.Request | str,
    *,
    timeout: float = 10,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep=time.sleep,
):
    """``urlopen(req)`` that retries on 429 (honoring Retry-After) and 5xx, then
    returns the decoded JSON body.

    ``sleep`` is injectable so tests can assert backoff without wall-clock waits.
    The last ``HTTPError`` (or other exception) is re-raised once ``max_retries``
    retryable responses have been seen.
    """
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code not in RETRYABLE_STATUS or attempt >= max_retries:
                raise
            body: object = {}
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                pass
            wait = _retry_after_seconds(e, body)
            if wait is None:
                # No server-provided delay (typical for 5xx) → exponential backoff.
                wait = float(min(2 ** attempt, MAX_SLEEP_SECONDS))
            # Small cushion over the server's figure, still bounded.
            wait = min(wait + 0.5, MAX_SLEEP_SECONDS)
            sleep(wait)
            attempt += 1

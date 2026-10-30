"""First-owner enrollment challenge shared by the chat bridges.

An unenrolled bridge prints a one-time code to its local log. A direct message
enrolls its sender as owner only when the whole message equals that code, the
code has not expired, it has not been used, and the failed-attempt budget is
not spent. Adapters own receiving the message and sending the reply.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Callable, List, Optional

CODE_BYTES = 16
DEFAULT_TTL_SECONDS = 60 * 60
DEFAULT_MAX_FAILURES = 5

ACCEPTED = "accepted"
REJECTED = "rejected"
CLOSED = "closed"

_REPLIES = {
    REJECTED: (
        "Enrollment code required.\n"
        "Send only the code from the bridge startup log, with nothing else in the message."
    ),
    CLOSED: (
        "Enrollment is closed.\n"
        "Restart the bridge on its host to print a new code, then send that code here."
    ),
}


class EnrollmentChallenge:
    """One-time owner enrollment code with an expiry and a failed-attempt budget."""

    def __init__(
        self,
        code: Optional[str] = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_failures: int = DEFAULT_MAX_FAILURES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.code = (code if code is not None else secrets.token_hex(CODE_BYTES)).lower()
        self.ttl_seconds = ttl_seconds
        self.max_failures = max_failures
        self._clock = clock
        self._issued_at = clock()
        self._failures = 0
        self._reserved = False
        self._used = False
        self._lock = threading.Lock()

    def _closed_locked(self) -> bool:
        return (
            self._reserved
            or self._used
            or self._failures >= self.max_failures
            or self._clock() - self._issued_at >= self.ttl_seconds
        )

    def check(self, text: Optional[str]) -> str:
        """Return ACCEPTED and reserve the code for one commit/release cycle, REJECTED, or CLOSED."""
        # Hex is case-free; lowercasing only forgives a phone keyboard's capital.
        candidate = (text or "").strip().lower().encode("utf-8")
        with self._lock:
            if self._closed_locked():
                return CLOSED
            if hmac.compare_digest(candidate, self.code.encode("utf-8")):
                self._reserved = True
                return ACCEPTED
            self._failures += 1
            return CLOSED if self._failures >= self.max_failures else REJECTED

    def commit(self) -> None:
        """Consume the reserved code after onboarding state is persisted."""
        with self._lock:
            self._used = True
            self._reserved = False

    def release(self) -> None:
        """Free a reservation after failed persistence so the code stays retryable."""
        with self._lock:
            self._reserved = False


def check_enrollment(challenge: Optional[EnrollmentChallenge], text: Optional[str]) -> str:
    """Verdict for an enrollment attempt; no challenge issued means enrollment is closed."""
    if challenge is None:
        return CLOSED
    return challenge.check(text)


def commit_enrollment(challenge: Optional[EnrollmentChallenge]) -> None:
    """Consume a reserved code once the caller persisted the new owner record."""
    if challenge is not None:
        challenge.commit()


def release_enrollment(challenge: Optional[EnrollmentChallenge]) -> None:
    """Free a reservation after failed persistence so the code stays retryable."""
    if challenge is not None:
        challenge.release()


def reply_for(verdict: str) -> str:
    """Reply text for a REJECTED or CLOSED verdict."""
    return _REPLIES[verdict]


def banner_lines(challenge: EnrollmentChallenge) -> List[str]:
    """Startup log lines that show the operator the code; only one names "enrollment code"."""
    minutes = max(1, int(challenge.ttl_seconds // 60))
    return [
        "",
        "  *** TOFU enrollment required ***",
        f"  Enrollment code: {challenge.code}",
        "  Send this code, and nothing else, as a direct message to register as owner.",
        f"  It is single-use and expires after {minutes} minutes or"
        f" {challenge.max_failures} wrong attempts; restart the bridge for a new one.",
        "  Anyone who sends this code first becomes owner — keep it private.",
        "",
    ]

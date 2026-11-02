"""A wall-clock bound for a call that must not be able to wait.

A test asserting "this returns rather than blocks" cannot express that with an
assertion: a blocking call never reaches one, so the regression shows up as a
hung suite instead of a red case. SIGALRM interrupts the blocking syscall
itself and the handler's exception propagates out of it (PEP 475), which turns
"waited" into an ordinary failure with a name.
"""

from __future__ import annotations

import contextlib
import signal


@contextlib.contextmanager
def deadline(seconds: float, what: str):
    """Fail `what` if the body has not finished within `seconds`."""
    def _fire(_signum, _frame):
        raise AssertionError(f"{what} did not return within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

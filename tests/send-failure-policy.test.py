#!/usr/bin/env python3
"""send_failure_policy: a blip retries, a rejection parks, an outage parks eventually."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import send_failure_policy as sfp


class FakeHTTPException(Exception):
    """Shaped like discord.HTTPException: an HTTP `.status` and a Discord `.code`."""

    def __init__(self, status, code=0):
        super().__init__(f"{status} (error code: {code})")
        self.status = status
        self.code = code


class TestIsTransient(unittest.TestCase):
    def test_503_is_transient(self):
        # The live case: `503 Service Unavailable (error code: 0): upstream connect
        # error or disconnect/reset before headers` parked a pending-questions digest
        # for 10.5h. It would have delivered on the next 3s poll.
        self.assertTrue(sfp.is_transient(FakeHTTPException(503)))

    def test_server_errors_and_rate_limit_are_transient(self):
        for status in (429, 500, 502, 503, 504, 408, 425):
            self.assertTrue(sfp.is_transient(FakeHTTPException(status)), status)

    def test_payload_and_permission_rejections_are_permanent(self):
        # 413/40005 is the case the quarantine was built for; 403 is "cannot DM this
        # user"; 400/50035 is a malformed body. None becomes a 200 on retry.
        for status, code in ((413, 40005), (403, 50007), (400, 50035), (404, 10003), (401, 0)):
            self.assertFalse(sfp.is_transient(FakeHTTPException(status, code)), status)

    def test_discord_error_code_is_not_read_as_an_http_status(self):
        # `.code` 50035 is not a status. If the classifier fell back to `.code` when
        # `.status` were absent, a permanent rejection could land in a transient
        # bucket by coincidence of numbering.
        exc = FakeHTTPException(400, 50035)
        self.assertEqual(sfp.failure_status(exc), 400)

    def test_connection_level_failures_are_transient(self):
        self.assertTrue(sfp.is_transient(TimeoutError("timed out")))
        self.assertTrue(sfp.is_transient(ConnectionResetError("reset by peer")))

        class ServerDisconnectedError(Exception):
            pass

        self.assertTrue(sfp.is_transient(ServerDisconnectedError()))

    def test_unknown_failure_parks(self):
        # The safe default: quarantine preserves the body AND health-check reports it,
        # so parking an unknown error loses nothing, while retrying it forever
        # would bury the log.
        self.assertFalse(sfp.is_transient(ValueError("something else entirely")))
        self.assertFalse(sfp.is_transient(FakeHTTPException(418)))

    def test_bool_status_is_not_an_int_status(self):
        class Odd(Exception):
            status = True

        self.assertIsNone(sfp.failure_status(Odd()))
        self.assertFalse(sfp.is_transient(Odd()))


class TestShouldRetry(unittest.TestCase):
    def test_retries_up_to_the_cap_then_parks(self):
        exc = FakeHTTPException(503)
        for attempts in range(sfp.MAX_TRANSIENT_ATTEMPTS):
            self.assertTrue(sfp.should_retry(exc, attempts), attempts)
        self.assertFalse(sfp.should_retry(exc, sfp.MAX_TRANSIENT_ATTEMPTS))
        self.assertFalse(sfp.should_retry(exc, sfp.MAX_TRANSIENT_ATTEMPTS + 7))

    def test_a_permanent_failure_never_retries_even_at_zero_attempts(self):
        self.assertFalse(sfp.should_retry(FakeHTTPException(413, 40005), 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)

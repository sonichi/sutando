#!/usr/bin/env python3
"""send_failure_policy: a blip retries, a rejection parks, an outage parks eventually."""
import sys
import tempfile
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
        # The live case: a 503 parked an owner-facing digest that the next 3s poll
        # would have delivered.
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
        # `.code` shares no numbering with an HTTP status, so reading it as one could
        # land a permanent rejection in the transient bucket.
        exc = FakeHTTPException(400, 50035)
        self.assertEqual(sfp.failure_status(exc), 400)

    def test_connection_level_failures_are_transient(self):
        self.assertTrue(sfp.is_transient(TimeoutError("timed out")))
        self.assertTrue(sfp.is_transient(ConnectionResetError("reset by peer")))

        class ServerDisconnectedError(Exception):
            pass

        self.assertTrue(sfp.is_transient(ServerDisconnectedError()))

    def test_a_subclass_of_a_listed_transient_is_transient(self):
        # aiohttp raises ClientConnectorDNSError(ClientConnectorError) for a DNS
        # failure, so the concrete name is NOT the listed one. Matching only the
        # concrete class parked an owner's DM permanently on the first blip —
        # the retry budget was never spent because the branch was never entered.
        class ClientConnectorError(Exception):
            pass

        class ClientConnectorDNSError(ClientConnectorError):
            pass

        exc = ClientConnectorDNSError(
            "Cannot connect to host discord.com:443 ssl:default "
            "[nodename nor servname provided, or not known]")
        self.assertNotIn(type(exc).__name__, sfp._TRANSIENT_EXC_NAMES)
        self.assertTrue(sfp.is_transient(exc))
        self.assertTrue(sfp.should_retry(exc, 0))

    def test_an_unlisted_hierarchy_still_parks(self):
        # The MRO walk must not turn "any exception" into a retry: only a listed
        # ancestor counts.
        class SomeLibraryError(Exception):
            pass

        class MalformedPayloadError(SomeLibraryError):
            pass

        self.assertFalse(sfp.is_transient(MalformedPayloadError("bad body")))

    def test_a_tls_failure_parks_even_though_its_parent_is_listed(self):
        # The MRO walk widens the transient set to everything under
        # ClientConnectorError, which on aiohttp 3.13.5 pulls in three TLS types.
        # A bad cert, wrong CA or pin mismatch is a misconfiguration: retrying
        # only burns the 5-attempt budget before parking anyway.
        class ClientConnectorError(Exception):
            pass

        class ClientSSLError(ClientConnectorError):
            pass

        class ClientConnectorCertificateError(ClientSSLError):
            pass

        class ClientConnectorSSLError(ClientSSLError):
            pass

        for cls in (ClientSSLError, ClientConnectorCertificateError,
                    ClientConnectorSSLError):
            exc = cls("certificate verify failed")
            self.assertFalse(sfp.is_transient(exc), cls.__name__)
            self.assertFalse(sfp.should_retry(exc, 0), cls.__name__)

    def test_a_pinned_fingerprint_mismatch_parks_via_a_different_ancestor(self):
        # ServerFingerprintMismatch is a TLS pin failure that reaches the
        # transient set through ServerConnectionError, NOT ClientSSLError — so
        # excluding the SSL branch alone would still have retried it.
        class ClientConnectionError(Exception):
            pass

        class ServerConnectionError(ClientConnectionError):
            pass

        class ServerFingerprintMismatch(ServerConnectionError):
            pass

        self.assertTrue(sfp.is_transient(ServerConnectionError("dropped")),
                        "the parent stays transient")
        self.assertFalse(sfp.is_transient(ServerFingerprintMismatch("pin")))

    def test_the_permanent_set_does_not_swallow_its_transient_siblings(self):
        # ClientProxyConnectionError and ClientConnectorDNSError descend from
        # ClientConnectorError WITHOUT passing through ClientSSLError, so the
        # exclusion must not reach them.
        class ClientConnectorError(Exception):
            pass

        class ClientProxyConnectionError(ClientConnectorError):
            pass

        class ClientConnectorDNSError(ClientConnectorError):
            pass

        self.assertTrue(sfp.is_transient(ClientProxyConnectionError("proxy down")))
        self.assertTrue(sfp.is_transient(ClientConnectorDNSError("dns")))

    def test_urlerror_wrapper_classified_by_its_reason(self):
        # urllib wraps the real failure; the wrapper alone reads as permanent.
        import socket
        import urllib.error
        for inner in (TimeoutError("t"), ConnectionRefusedError(),
                      socket.gaierror(8, "nodename nor servname")):
            self.assertTrue(sfp.is_transient(urllib.error.URLError(inner)), inner)
        self.assertFalse(sfp.is_transient(urllib.error.URLError("not an exception")))

    def test_nested_reason_chain_unwraps_but_is_bounded(self):
        import urllib.error
        nested = urllib.error.URLError(urllib.error.URLError(TimeoutError("t")))
        self.assertTrue(sfp.is_transient(nested))
        # deeper than the unwrap bound: parks rather than loops
        deep = ValueError("leaf")
        for _ in range(6):
            deep = urllib.error.URLError(deep)
        self.assertFalse(sfp.is_transient(deep))
        # cyclic .reason must not hang
        cyc = urllib.error.URLError("x")
        cyc.reason = cyc
        self.assertFalse(sfp.is_transient(cyc))

    def test_unknown_failure_parks(self):
        # Parking an unknown error loses nothing — quarantine preserves the body and
        # health-check reports it — while retrying forever would bury the log.
        self.assertFalse(sfp.is_transient(ValueError("something else entirely")))
        self.assertFalse(sfp.is_transient(FakeHTTPException(418)))

    def test_bool_status_is_not_an_int_status(self):
        class Odd(Exception):
            status = True

        self.assertIsNone(sfp.failure_status(Odd()))
        self.assertFalse(sfp.is_transient(Odd()))


class TestServerErrorRange(unittest.TestCase):
    """A named server error carrying an unenumerated status must not park.

    `DiscordServerError` is in _TRANSIENT_EXC_NAMES, but the status branch returned
    first, so any status outside the enumerated four made the name unreachable.
    """

    class DiscordServerError(Exception):
        def __init__(self, status=None):
            super().__init__(str(status))
            if status is not None:
                self.status = status

    def test_a_524_is_retried_not_parked(self):
        e = self.DiscordServerError(524)
        self.assertTrue(sfp.is_transient(e))
        self.assertTrue(sfp.should_retry(e, 0))

    def test_the_cloudflare_range_discord_sits_behind_is_transient(self):
        for status in range(520, 528):
            self.assertTrue(sfp.is_transient(self.DiscordServerError(status)), status)

    def test_the_whole_5xx_range_is_transient(self):
        for status in (500, 505, 507, 511, 550, 599):
            self.assertTrue(sfp.is_transient(FakeHTTPException(status)), status)

    def test_the_name_is_not_shadowed_by_an_unenumerated_status(self):
        # The mechanism, stated separately from the 5xx range: a named-transient type
        # stays transient even at a status the range would not cover.
        self.assertTrue(sfp.is_transient(self.DiscordServerError(418)))

    def test_permanent_4xx_still_parks(self):
        # The range must not have widened into the rejection statuses.
        for status in (400, 401, 403, 404, 413, 422, 451):
            self.assertFalse(sfp.is_transient(FakeHTTPException(status)), status)


class TestShouldRetry(unittest.TestCase):
    def test_retries_up_to_the_cap_then_parks(self):
        exc = FakeHTTPException(503)
        for attempts in range(sfp.MAX_TRANSIENT_ATTEMPTS):
            self.assertTrue(sfp.should_retry(exc, attempts), attempts)
        self.assertFalse(sfp.should_retry(exc, sfp.MAX_TRANSIENT_ATTEMPTS))
        self.assertFalse(sfp.should_retry(exc, sfp.MAX_TRANSIENT_ATTEMPTS + 7))

    def test_a_permanent_failure_never_retries_even_at_zero_attempts(self):
        self.assertFalse(sfp.should_retry(FakeHTTPException(413, 40005), 0))


class TestResolveFailedSend(unittest.TestCase):
    """The decision and the file move are one unit — test the FILESYSTEM outcome."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.claim = self.d / "proactive-x.sending"
        self.claim.write_text("owner body")
        self.attempts = {}

    def test_transient_returns_the_body_to_the_polled_name(self):
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(503), self.attempts)
        self.assertEqual(out, "retried")
        self.assertTrue((self.d / "proactive-x.txt").is_file(),
                        "a retried body must be re-pollable as .txt")
        self.assertFalse(self.claim.exists())
        self.assertEqual((self.d / "proactive-x.txt").read_text(), "owner body")
        self.assertEqual(self.attempts, {"proactive-x.txt": 1})

    def test_permanent_parks_under_undelivered_with_the_txt_name(self):
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(413, 40005), self.attempts)
        self.assertEqual(out, "parked")
        parked = self.d / "undelivered" / "proactive-x.txt"
        self.assertTrue(parked.is_file(), "must park under undelivered/")
        self.assertEqual(parked.read_text(), "owner body", "the body must survive intact")
        # A quarantined `*.sending` would read as in-flight to the restart sweep.
        self.assertFalse((self.d / "undelivered" / "proactive-x.sending").exists())

    def test_the_cap_converts_a_transient_into_a_park(self):
        self.attempts["proactive-x.txt"] = sfp.MAX_TRANSIENT_ATTEMPTS
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(503), self.attempts)
        self.assertEqual(out, "parked", "an endless outage must stop re-polling")
        self.assertNotIn("proactive-x.txt", self.attempts, "counter must not leak")

    def test_a_newer_body_is_never_clobbered_by_the_retry(self):
        # release_claim refuses when a .txt reappeared since the claim; the older
        # body must then park, not vanish.
        (self.d / "proactive-x.txt").write_text("newer body")
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(503), self.attempts)
        self.assertEqual(out, "parked")
        self.assertEqual((self.d / "proactive-x.txt").read_text(), "newer body")
        self.assertEqual((self.d / "undelivered" / "proactive-x.txt").read_text(), "owner body")

    def test_a_body_is_never_deleted_even_when_the_move_fails(self):
        # undelivered/ occupied by a FILE, so mkdir raises -> "stuck", body in place.
        (self.d / "undelivered").write_text("not a directory")
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(413), self.attempts)
        self.assertEqual(out, "stuck")
        self.assertTrue(self.claim.is_file(), "left in place rather than lost")

    def test_repeated_transients_count_up_per_body(self):
        for expected in (1, 2, 3):
            claim = self.d / "proactive-y.sending"
            claim.write_text("b")
            self.assertEqual(sfp.resolve_failed_send(claim, FakeHTTPException(503), self.attempts),
                             "retried")
            (self.d / "proactive-y.txt").rename(claim)   # simulate the next poll's claim
            self.assertEqual(self.attempts["proactive-y.txt"], expected)


class TestPartialDeliveryNeverRetries(unittest.TestCase):
    """A retry is only safe from ZERO progress.

    The bridge's except wraps the chunk loop AND the attachment loop, so releasing
    the claim after any successful send resends the whole body and repeats what
    already reached the owner — the duplicate-delivery failure claim-by-rename
    exists to prevent. Reviewer reproduced it: released_after_partial=True,
    first_chunk_duplicated=True.
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.claim = self.d / "proactive-x.sending"
        self.claim.write_text("chunk1 chunk2")
        self.attempts = {}

    def test_transient_AFTER_a_partial_send_parks_instead_of_retrying(self):
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(503),
                                      self.attempts, progressed=True)
        self.assertEqual(out, "parked", "a partly-delivered body must never be re-sent")
        self.assertFalse((self.d / "proactive-x.txt").exists(),
                         "released .txt would be re-polled and re-sent in full")
        self.assertTrue((self.d / "undelivered" / "proactive-x.txt").is_file())

    def test_transient_with_ZERO_progress_still_retries(self):
        out = sfp.resolve_failed_send(self.claim, FakeHTTPException(503),
                                      self.attempts, progressed=False)
        self.assertEqual(out, "retried")
        self.assertTrue((self.d / "proactive-x.txt").is_file())

    def test_progressed_defaults_to_false_so_callers_opt_IN_to_parking(self):
        # A new caller that forgets the flag keeps the old retry behaviour rather
        # than silently parking every transient failure.
        self.assertEqual(sfp.resolve_failed_send(self.claim, FakeHTTPException(503),
                                                 self.attempts), "retried")

    def test_a_permanent_failure_parks_regardless_of_progress(self):
        for progressed in (True, False):
            d = Path(tempfile.mkdtemp())
            c = d / "proactive-y.sending"
            c.write_text("b")
            self.assertEqual(
                sfp.resolve_failed_send(c, FakeHTTPException(413, 40005), {},
                                        progressed=progressed), "parked", progressed)


class TestApprovalMarkerCapIsEnforced(unittest.TestCase):
    """The approval branch used is_transient with no cap — unbounded 3s hot loop.

    Reviewer reproduced 7 transient failures against a cap of 5 with the marker
    still hot. The bridge now uses should_retry with per-marker accounting, so the
    contract is the same one the policy already enforces.
    """

    def test_should_retry_stops_at_the_cap_for_a_marker_key(self):
        exc = FakeHTTPException(503)
        attempts = {}
        key = "approved-123"
        fired = 0
        for _ in range(9):
            if sfp.should_retry(exc, attempts.get(key, 0)):
                attempts[key] = attempts.get(key, 0) + 1
                fired += 1
        self.assertEqual(fired, sfp.MAX_TRANSIENT_ATTEMPTS,
                         "an outage must stop retrying, not loop forever")

    def test_the_bridge_uses_should_retry_not_bare_is_transient_for_approvals(self):
        src = (Path(__file__).resolve().parent.parent / "src" / "discord-bridge.py").read_text()
        window = src.split("Failed to send approval to", 1)[1][:900]
        self.assertIn("should_retry", window, "the approval branch must be capped")
        self.assertIn("MAX_TRANSIENT_ATTEMPTS", window, "and must say so in its log")


if __name__ == "__main__":
    unittest.main(verbosity=2)

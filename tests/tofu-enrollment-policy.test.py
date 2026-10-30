#!/usr/bin/env python3
"""Tests for the shared first-owner enrollment challenge policy."""

from __future__ import annotations

import re
import secrets
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import tofu_enrollment  # noqa: E402


def test_codes_are_128_bit_lowercase_hex_and_unique():
    first = tofu_enrollment.EnrollmentChallenge()
    second = tofu_enrollment.EnrollmentChallenge()
    assert re.fullmatch(r"[0-9a-f]{32}", first.code)
    assert re.fullmatch(r"[0-9a-f]{32}", second.code)
    assert first.code != second.code


def test_accepts_exact_and_trimmed_case_insensitive_code():
    challenge = tofu_enrollment.EnrollmentChallenge()
    assert tofu_enrollment.check_enrollment(challenge, challenge.code) == tofu_enrollment.ACCEPTED

    challenge = tofu_enrollment.EnrollmentChallenge()
    text = "  " + challenge.code.upper() + "\n"
    assert tofu_enrollment.check_enrollment(challenge, text) == tofu_enrollment.ACCEPTED


def test_rejects_code_inside_longer_text():
    challenge = tofu_enrollment.EnrollmentChallenge()
    assert tofu_enrollment.check_enrollment(
        challenge, "my code is " + challenge.code
    ) == tofu_enrollment.REJECTED

    challenge = tofu_enrollment.EnrollmentChallenge()
    text = "".join(secrets.token_hex(3) for _ in range(500)) + challenge.code
    assert tofu_enrollment.check_enrollment(challenge, text) == tofu_enrollment.REJECTED


def test_acceptance_is_single_use():
    challenge = tofu_enrollment.EnrollmentChallenge()
    assert challenge.check(challenge.code) == tofu_enrollment.ACCEPTED
    assert challenge.check(challenge.code) == tofu_enrollment.CLOSED


def test_failure_budget_closes_on_fifth_attempt():
    challenge = tofu_enrollment.EnrollmentChallenge(max_failures=5)
    for _ in range(4):
        assert challenge.check("wrong") == tofu_enrollment.REJECTED
    assert challenge.check("wrong") == tofu_enrollment.CLOSED
    assert challenge.check(challenge.code) == tofu_enrollment.CLOSED


def test_expiry_boundary():
    now = [100.0]
    expired = tofu_enrollment.EnrollmentChallenge(
        ttl_seconds=60, clock=lambda: now[0]
    )
    now[0] = 160.0
    assert expired.check(expired.code) == tofu_enrollment.CLOSED

    now[0] = 100.0
    valid = tofu_enrollment.EnrollmentChallenge(
        ttl_seconds=60, clock=lambda: now[0]
    )
    now[0] = 159.0
    assert valid.check(valid.code) == tofu_enrollment.ACCEPTED


def test_closed_attempts_do_not_change_failure_state():
    challenge = tofu_enrollment.EnrollmentChallenge(max_failures=1)
    assert challenge.check("wrong") == tofu_enrollment.CLOSED
    failures = challenge._failures
    assert challenge.check(challenge.code) == tofu_enrollment.CLOSED
    assert challenge._failures == failures


def test_missing_challenge_closes_and_missing_text_is_rejected():
    assert tofu_enrollment.check_enrollment(None, "anything") == tofu_enrollment.CLOSED
    challenge = tofu_enrollment.EnrollmentChallenge()
    assert challenge.check(None) == tofu_enrollment.REJECTED


def test_comparison_uses_constant_time_helper():
    challenge = tofu_enrollment.EnrollmentChallenge()
    original = tofu_enrollment.hmac.compare_digest
    calls: list[tuple[bytes, bytes]] = []

    def recording_compare_digest(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return original(left, right)

    tofu_enrollment.hmac.compare_digest = recording_compare_digest
    try:
        assert challenge.check(challenge.code) == tofu_enrollment.ACCEPTED
    finally:
        tofu_enrollment.hmac.compare_digest = original
    assert calls


def test_concurrent_attempts_accept_exactly_once():
    challenge = tofu_enrollment.EnrollmentChallenge()
    barrier = threading.Barrier(32)
    verdicts: list[str] = []
    verdicts_lock = threading.Lock()

    def attempt():
        barrier.wait()
        verdict = challenge.check(challenge.code)
        with verdicts_lock:
            verdicts.append(verdict)

    threads = [threading.Thread(target=attempt) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert verdicts.count(tofu_enrollment.ACCEPTED) == 1
    assert verdicts.count(tofu_enrollment.CLOSED) == 31


def test_banner_and_replies():
    challenge = tofu_enrollment.EnrollmentChallenge()
    lines = tofu_enrollment.banner_lines(challenge)
    matching = [line for line in lines if re.search("enrollment code", line, re.I)]
    assert len(matching) == 1
    assert challenge.code in matching[0]
    assert "Enrollment code required" in tofu_enrollment.reply_for(tofu_enrollment.REJECTED)
    assert "Restart the bridge" in tofu_enrollment.reply_for(tofu_enrollment.CLOSED)


def main() -> int:
    tests = [
        ("generates unique lowercase 128-bit codes", test_codes_are_128_bit_lowercase_hex_and_unique),
        ("accepts exact and trimmed uppercase codes", test_accepts_exact_and_trimmed_case_insensitive_code),
        ("rejects codes embedded in longer messages", test_rejects_code_inside_longer_text),
        ("acceptance is single-use", test_acceptance_is_single_use),
        ("closes after five failed attempts", test_failure_budget_closes_on_fifth_attempt),
        ("enforces the expiry boundary", test_expiry_boundary),
        ("closed attempts leave failure state unchanged", test_closed_attempts_do_not_change_failure_state),
        ("closes missing challenges and rejects missing text", test_missing_challenge_closes_and_missing_text_is_rejected),
        ("uses constant-time code comparison", test_comparison_uses_constant_time_helper),
        ("accepts concurrent attempts exactly once", test_concurrent_attempts_accept_exactly_once),
        ("prints one code line and usable replies", test_banner_and_replies),
    ]
    failures = 0
    for label, fn in tests:
        try:
            fn()
            print(f"PASS: {label}")
        except AssertionError as e:
            print(f"FAIL: {label} — {e}", file=sys.stderr)
            failures += 1
        except Exception as e:
            print(f"ERROR: {label} — {type(e).__name__}: {e}", file=sys.stderr)
            failures += 1

    if failures:
        print(f"\n{failures}/{len(tests)} tests failed.", file=sys.stderr)
        return 1
    print(f"\nAll {len(tests)} enrollment policy tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

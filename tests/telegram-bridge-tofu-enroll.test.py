#!/usr/bin/env python3
"""Functional tests for Telegram's first-owner enrollment gate.

Private messages must contain the entire current code. The shared challenge
policy closes missing, expired, used, and exhausted challenges.

Run: python3 tests/telegram-bridge-tofu-enroll.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import importlib.util
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE_SRC = REPO / "src" / "telegram-bridge.py"


def _load_bridge():
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-placeholder-token")
    spec = importlib.util.spec_from_file_location(
        "telegram_bridge_tofu_enroll", BRIDGE_SRC
    )
    sys.path.insert(0, str(REPO / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BRIDGE = _load_bridge()
SRC = BRIDGE_SRC.read_text()


def _main_body() -> str:
    pos = SRC.find("def main():")
    assert pos != -1, "def main(): not found in telegram-bridge.py"
    return SRC[pos: pos + 6000]


def _new_challenge(code: str = "a1b2c3d4e5f60718293a4b5c6d7e8f90"):
    BRIDGE._TOFU_ENROLLMENT = BRIDGE.tofu_enrollment.EnrollmentChallenge(code)
    return BRIDGE._TOFU_ENROLLMENT


def test_exact_private_message_enrolls_owner():
    challenge = _new_challenge()
    onboard_calls: list[tuple] = []
    original_onboard = BRIDGE.tofu_onboard
    BRIDGE.tofu_onboard = lambda uid, username: onboard_calls.append((uid, username)) or {uid}
    try:
        result = BRIDGE._admit_first_owner(
            "123", "owner", 123, True, challenge.code
        )
    finally:
        BRIDGE.tofu_onboard = original_onboard
        BRIDGE._TOFU_ENROLLMENT = None
    assert result == {"123"}
    assert onboard_calls == [("123", "owner")]


def test_code_embedded_in_longer_message_is_rejected():
    challenge = _new_challenge()
    replies: list[dict] = []
    onboard_calls: list[tuple] = []
    original_api = BRIDGE.api
    original_onboard = BRIDGE.tofu_onboard
    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    BRIDGE.tofu_onboard = lambda uid, username: onboard_calls.append((uid, username)) or {uid}
    try:
        result = BRIDGE._admit_first_owner(
            "123", "owner", 123, True, "my code is " + challenge.code
        )
    finally:
        BRIDGE.api = original_api
        BRIDGE.tofu_onboard = original_onboard
        BRIDGE._TOFU_ENROLLMENT = None
    assert result is None
    assert onboard_calls == []
    assert "Enrollment code required" in replies[0]["text"]


def test_non_private_message_does_not_spend_challenge():
    challenge = _new_challenge()
    replies: list[dict] = []
    onboard_calls: list[tuple] = []
    original_api = BRIDGE.api
    original_onboard = BRIDGE.tofu_onboard
    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    BRIDGE.tofu_onboard = lambda uid, username: onboard_calls.append((uid, username)) or {uid}
    try:
        result = BRIDGE._admit_first_owner(
            "123", "owner", 456, False, challenge.code
        )
        assert result is None
        assert replies == []
        assert onboard_calls == []
        result = BRIDGE._admit_first_owner(
            "123", "owner", 123, True, challenge.code
        )
    finally:
        BRIDGE.api = original_api
        BRIDGE.tofu_onboard = original_onboard
        BRIDGE._TOFU_ENROLLMENT = None
    assert result == {"123"}
    assert onboard_calls == [("123", "owner")]


def test_missing_challenge_returns_closed_reply():
    BRIDGE._TOFU_ENROLLMENT = None
    replies: list[dict] = []
    onboard_calls: list[tuple] = []
    original_api = BRIDGE.api
    original_onboard = BRIDGE.tofu_onboard
    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    BRIDGE.tofu_onboard = lambda uid, username: onboard_calls.append((uid, username)) or {uid}
    try:
        result = BRIDGE._admit_first_owner("123", "owner", 123, True, "hello")
    finally:
        BRIDGE.api = original_api
        BRIDGE.tofu_onboard = original_onboard
    assert result is None
    assert onboard_calls == []
    assert replies[-1]["text"] == BRIDGE.tofu_enrollment.reply_for(BRIDGE.tofu_enrollment.CLOSED)


def test_five_wrong_attempts_close_enrollment():
    challenge = _new_challenge()
    replies: list[dict] = []
    onboard_calls: list[tuple] = []
    original_api = BRIDGE.api
    original_onboard = BRIDGE.tofu_onboard
    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    BRIDGE.tofu_onboard = lambda uid, username: onboard_calls.append((uid, username)) or {uid}
    try:
        for _ in range(5):
            assert BRIDGE._admit_first_owner(
                "456", "attacker", 456, True, "wrong"
            ) is None
        assert BRIDGE._admit_first_owner(
            "456", "attacker", 456, True, challenge.code
        ) is None
    finally:
        BRIDGE.api = original_api
        BRIDGE.tofu_onboard = original_onboard
        BRIDGE._TOFU_ENROLLMENT = None
    assert onboard_calls == []
    assert replies[-1]["text"] == BRIDGE.tofu_enrollment.reply_for(BRIDGE.tofu_enrollment.CLOSED)


def test_enrollment_check_delegates_to_policy():
    challenge = _new_challenge()
    calls: list[tuple] = []
    replies: list[dict] = []
    original_check = BRIDGE.tofu_enrollment.check_enrollment
    original_api = BRIDGE.api
    BRIDGE.tofu_enrollment.check_enrollment = (
        lambda current, text: calls.append((current, text))
        or BRIDGE.tofu_enrollment.REJECTED
    )
    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    try:
        result = BRIDGE._admit_first_owner("123", "owner", 123, True, "delegated")
    finally:
        BRIDGE.tofu_enrollment.check_enrollment = original_check
        BRIDGE.api = original_api
        BRIDGE._TOFU_ENROLLMENT = None
    assert result is None
    assert calls == [(challenge, "delegated")]


def test_failed_onboard_releases_the_code_for_retry():
    challenge = _new_challenge()
    calls: list[tuple] = []
    replies: list[dict] = []
    original_api = BRIDGE.api
    original_onboard = BRIDGE.tofu_onboard
    state = {"fail": True}

    def fake_onboard(uid, username):
        calls.append((uid, username))
        if state["fail"]:
            raise OSError("disk full")
        return {uid}

    BRIDGE.api = lambda method, **params: replies.append({"method": method, **params})
    BRIDGE.tofu_onboard = fake_onboard
    try:
        try:
            BRIDGE._admit_first_owner("123", "owner", 123, True, challenge.code)
            raise AssertionError("first attempt should propagate the write failure")
        except OSError:
            pass
        # The reservation must have been released: the same code can retry,
        # and a concurrent sender cannot steal the slot in between.
        assert challenge.check(challenge.code) == BRIDGE.tofu_enrollment.ACCEPTED
        challenge.release()
        state["fail"] = False
        result = BRIDGE._admit_first_owner("123", "owner", 123, True, challenge.code)
        assert result == {"123"}
        assert challenge.check(challenge.code) == BRIDGE.tofu_enrollment.CLOSED
    finally:
        BRIDGE.api = original_api
        BRIDGE.tofu_onboard = original_onboard
        BRIDGE._TOFU_ENROLLMENT = None
    assert calls == [("123", "owner"), ("123", "owner")]


def test_init_enrollment_issues_and_announces_code_only_when_unenrolled(capsys=None):
    import io
    import contextlib
    import tempfile

    original_access = BRIDGE.ACCESS_FILE
    BRIDGE._TOFU_ENROLLMENT = None
    try:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "access.json"
            BRIDGE.ACCESS_FILE = missing
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                challenge = BRIDGE._init_enrollment()
            assert isinstance(challenge, BRIDGE.tofu_enrollment.EnrollmentChallenge)
            assert BRIDGE._TOFU_ENROLLMENT is challenge
            assert f"Enrollment code: {challenge.code}" in buf.getvalue()

            missing.write_text('{"allowFrom": ["1"]}')
            BRIDGE._TOFU_ENROLLMENT = None
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                assert BRIDGE._init_enrollment() is None
            assert BRIDGE._TOFU_ENROLLMENT is None
            assert "Enrollment code" not in buf.getvalue()
    finally:
        BRIDGE.ACCESS_FILE = original_access
        BRIDGE._TOFU_ENROLLMENT = None


def test_poll_loop_calls_first_owner_helper():
    assert "_admit_first_owner(" in _main_body()


def main() -> int:
    tests = [
        ("accepts an exact private-message code", test_exact_private_message_enrolls_owner),
        ("rejects a code embedded in longer text", test_code_embedded_in_longer_message_is_rejected),
        ("ignores non-private messages without spending the challenge", test_non_private_message_does_not_spend_challenge),
        ("closes when no challenge exists", test_missing_challenge_returns_closed_reply),
        ("closes after five wrong attempts", test_five_wrong_attempts_close_enrollment),
        ("delegates enrollment checks to shared policy", test_enrollment_check_delegates_to_policy),
        ("releases the code for retry after a failed onboard", test_failed_onboard_releases_the_code_for_retry),
        ("issues the startup code only when unenrolled", test_init_enrollment_issues_and_announces_code_only_when_unenrolled),
        ("uses the enrollment helper in the poll loop", test_poll_loop_calls_first_owner_helper),
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
    print(f"\nAll {len(tests)} Telegram enrollment tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

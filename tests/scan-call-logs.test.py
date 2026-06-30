#!/usr/bin/env python3
"""Tests for the detection functions in src/scan-call-logs.py.

These functions are pure (transcript string → list of issue dicts) and
critical for post-call quality monitoring. Tests cover both positive
(issue found) and negative (clean transcript) cases.

Run: python3 tests/scan-call-logs.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("scan_call_logs", REPO / "src" / "scan-call-logs.py")
scl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scl)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


def _has_pattern(issues: list[dict], pattern: str) -> bool:
    return any(i.get("pattern") == pattern for i in issues)


# ---------------------------------------------------------------------------
# detect_duplicate_responses
# ---------------------------------------------------------------------------

def test_duplicate_responses_detects_consecutive() -> list[str]:
    """Consecutive identical Sutando responses should be flagged."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Tell me about the meeting.",
        "Sutando: The meeting is at 3pm tomorrow.",
        "Sutando: The meeting is at 3pm tomorrow.",
    ])
    issues = scl.detect_duplicate_responses(transcript)
    check("consecutive duplicate not detected", _has_pattern(issues, "duplicate_response"), fails)
    return fails


def test_duplicate_responses_ignores_spread() -> list[str]:
    """Duplicates spread across many turns (intentional repeats) are NOT flagged."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: What time?",
        "Sutando: The meeting is at 3pm.",
        "Recipient: And after that?",
        "Sutando: You have a call at 5pm.",
        "Recipient: What time again?",
        "Sutando: The meeting is at 3pm.",
    ])
    issues = scl.detect_duplicate_responses(transcript)
    check("spread duplicates wrongly flagged", not _has_pattern(issues, "duplicate_response"), fails)
    return fails


def test_duplicate_responses_clean_transcript() -> list[str]:
    """Transcript with no duplicates produces no issues."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Hey Sutando.",
        "Sutando: Hi! How can I help?",
        "Recipient: What's the weather?",
        "Sutando: It's sunny and 72°F.",
    ])
    issues = scl.detect_duplicate_responses(transcript)
    check("clean transcript got issues", len(issues) == 0, fails)
    return fails


# ---------------------------------------------------------------------------
# detect_access_issues
# ---------------------------------------------------------------------------

def test_access_issues_detects_cant_access() -> list[str]:
    """'I can't access' lines are flagged as access issues."""
    fails: list[str] = []
    transcript = "Sutando: I can't access your calendar right now."
    issues = scl.detect_access_issues(transcript)
    check("access denied not detected", len(issues) > 0, fails)
    return fails


def test_access_issues_detects_not_authorized() -> list[str]:
    """'not authorized' variant is flagged."""
    fails: list[str] = []
    transcript = "Sutando: That operation isn't authorized."
    issues = scl.detect_access_issues(transcript)
    check("not authorized not detected", len(issues) > 0, fails)
    return fails


def test_access_issues_clean_transcript() -> list[str]:
    """Transcript with no access issues returns empty list."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Send that email.",
        "Sutando: Done, email sent to chi@example.com.",
    ])
    issues = scl.detect_access_issues(transcript)
    check("clean transcript got access issues", len(issues) == 0, fails)
    return fails


# ---------------------------------------------------------------------------
# detect_fabrication
# ---------------------------------------------------------------------------

def test_fabrication_detects_address() -> list[str]:
    """Agent claiming a specific street address triggers fabrication flag."""
    fails: list[str] = []
    transcript = "Sutando: The office is located at 123 Main St."
    issues = scl.detect_fabrication(transcript)
    check("fabricated address not detected", _has_pattern(issues, "potential_fabrication"), fails)
    return fails


def test_fabrication_detects_dollar_amount() -> list[str]:
    """Agent claiming a specific dollar balance triggers fabrication flag."""
    fails: list[str] = []
    transcript = "Sutando: Your balance is $4,832."
    issues = scl.detect_fabrication(transcript)
    check("fabricated dollar amount not detected", _has_pattern(issues, "potential_fabrication"), fails)
    return fails


def test_fabrication_clean_transcript() -> list[str]:
    """Normal responses without fabrication markers return empty list."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: What's on my calendar?",
        "Sutando: You have a 3pm meeting with the team.",
    ])
    issues = scl.detect_fabrication(transcript)
    check("clean transcript got fabrication issues", len(issues) == 0, fails)
    return fails


# ---------------------------------------------------------------------------
# detect_reconnect_leak
# ---------------------------------------------------------------------------

def test_reconnect_leak_detects_im_back() -> list[str]:
    """'I'm back' after reconnect should be detected."""
    fails: list[str] = []
    transcript = "Sutando: I'm back! What were we talking about?"
    issues = scl.detect_reconnect_leak(transcript)
    check("reconnect leak not detected", _has_pattern(issues, "reconnect_leak"), fails)
    return fails


def test_reconnect_leak_detects_welcome_back() -> list[str]:
    """'Welcome back' variant is also flagged."""
    fails: list[str] = []
    transcript = "Sutando: Welcome back! How can I help?"
    issues = scl.detect_reconnect_leak(transcript)
    check("welcome back not detected", _has_pattern(issues, "reconnect_leak"), fails)
    return fails


def test_reconnect_leak_case_insensitive() -> list[str]:
    """Detection is case-insensitive."""
    fails: list[str] = []
    transcript = "Sutando: I AM BACK, ready to assist."
    issues = scl.detect_reconnect_leak(transcript)
    check("case-insensitive reconnect not detected", _has_pattern(issues, "reconnect_leak"), fails)
    return fails


def test_reconnect_leak_clean_transcript() -> list[str]:
    """Normal reconnect-unrelated transcript produces no issues."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Sutando, help me draft an email.",
        "Sutando: Sure! Who should I address it to?",
    ])
    issues = scl.detect_reconnect_leak(transcript)
    check("clean transcript got reconnect issues", len(issues) == 0, fails)
    return fails


def test_reconnect_leak_recipient_not_flagged() -> list[str]:
    """'I'm back' said by the Recipient (not Sutando) should NOT be flagged."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: I'm back from lunch, what did I miss?",
        "Sutando: Welcome! Nothing major happened.",
    ])
    issues = scl.detect_reconnect_leak(transcript)
    # The recipient saying "I'm back" should not trigger the reconnect flag
    # (Sutando said "Welcome!" which matches; this checks the Sutando line filter)
    # Note: "Welcome!" alone doesn't match "Welcome back" pattern — verify
    check("recipient 'I'm back' wrongly flagged", not _has_pattern(issues, "reconnect_leak"), fails)
    return fails


# ---------------------------------------------------------------------------
# detect_repeated_command
# ---------------------------------------------------------------------------

def test_repeated_command_summon() -> list[str]:
    """User asking to summon 3+ times triggers repeated-command flag."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Summon the screen.",
        "Sutando: Summoning now.",
        "Recipient: Summon it please.",
        "Sutando: On it.",
        "Recipient: Can you summon?",
        "Sutando: Done.",
    ])
    issues = scl.detect_repeated_command(transcript)
    check("repeated summon not detected", _has_pattern(issues, "repeated_summon"), fails)
    return fails


def test_repeated_command_clean() -> list[str]:
    """Single summon does not trigger the repeated-command flag."""
    fails: list[str] = []
    transcript = "\n".join([
        "Recipient: Summon the screen.",
        "Sutando: Done.",
    ])
    issues = scl.detect_repeated_command(transcript)
    check("single summon wrongly flagged", not _has_pattern(issues, "repeated_summon"), fails)
    return fails


# ---------------------------------------------------------------------------
# detect_identity_confusion (if available)
# ---------------------------------------------------------------------------

def test_identity_confusion_if_present() -> list[str]:
    """detect_identity_confusion exists and returns a list (not a crash)."""
    fails: list[str] = []
    if not hasattr(scl, "detect_identity_confusion"):
        return fails  # not present, skip
    # Should return a list (empty or with issues)
    result = scl.detect_identity_confusion("Sutando: I am Bassil and I approve this.")
    check("detect_identity_confusion returned non-list", isinstance(result, list), fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("duplicate_responses: consecutive flagged", test_duplicate_responses_detects_consecutive),
        ("duplicate_responses: spread not flagged", test_duplicate_responses_ignores_spread),
        ("duplicate_responses: clean → no issues", test_duplicate_responses_clean_transcript),
        ("access_issues: can't access", test_access_issues_detects_cant_access),
        ("access_issues: not authorized", test_access_issues_detects_not_authorized),
        ("access_issues: clean → no issues", test_access_issues_clean_transcript),
        ("fabrication: street address", test_fabrication_detects_address),
        ("fabrication: dollar amount", test_fabrication_detects_dollar_amount),
        ("fabrication: clean → no issues", test_fabrication_clean_transcript),
        ("reconnect_leak: I'm back", test_reconnect_leak_detects_im_back),
        ("reconnect_leak: welcome back", test_reconnect_leak_detects_welcome_back),
        ("reconnect_leak: case-insensitive", test_reconnect_leak_case_insensitive),
        ("reconnect_leak: clean → no issues", test_reconnect_leak_clean_transcript),
        ("reconnect_leak: recipient not flagged", test_reconnect_leak_recipient_not_flagged),
        ("repeated_command: summon 3x", test_repeated_command_summon),
        ("repeated_command: once → clean", test_repeated_command_clean),
        ("identity_confusion: no crash", test_identity_confusion_if_present),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nscan-call-logs: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Tests for src/scan-call-logs.py

All detect_*() functions are pure (string/dict in, list out) — no file I/O.
Covers every detector with a trigger transcript and a clean no-issue transcript.
Also covers detect_metadata_issues() and scan_entry().

Run: python3 tests/scan-call-logs.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "scan_call_logs", REPO / "src" / "scan-call-logs.py"
)
scl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scl)

PASS = 0
FAIL = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {label}")


def fail(label: str, msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {label}: {msg}")


def _has_pattern(issues: list, pattern: str) -> bool:
    return any(i.get("pattern", "").startswith(pattern) for i in issues)


# ── detect_duplicate_responses ───────────────────────────────────────────────

def test_duplicate_responses_triggered():
    t = "\n".join([
        "Sutando: I've scheduled the meeting for 3pm tomorrow.",
        "Sutando: I've scheduled the meeting for 3pm tomorrow.",
        "Sutando: I've scheduled the meeting for 3pm tomorrow.",
    ])
    issues = scl.detect_duplicate_responses(t)
    if _has_pattern(issues, "duplicate_response"):
        ok("(dr-a) consecutive identical Sutando turns → duplicate_response")
    else:
        fail("(dr-a) consecutive identical Sutando turns → duplicate_response", f"got {issues}")


def test_duplicate_responses_no_flag_for_distinct_turns():
    t = "\n".join([
        "Sutando: Hello, how can I help you?",
        "Sutando: Sure, let me look that up.",
        "Sutando: I've found the result for you.",
    ])
    issues = scl.detect_duplicate_responses(t)
    if not issues:
        ok("(dr-b) distinct Sutando turns → no duplicate flag")
    else:
        fail("(dr-b) distinct Sutando turns → no duplicate flag", f"got {issues}")


# ── detect_access_issues ─────────────────────────────────────────────────────

def test_access_issues_cant_access():
    issues = scl.detect_access_issues("Sutando: I can't access that calendar.")
    if _has_pattern(issues, "access_issue"):
        ok("(ai-a) \"I can't access\" → access_issue detected")
    else:
        fail("(ai-a) \"I can't access\" → access_issue", f"got {issues}")


def test_access_issues_clean():
    issues = scl.detect_access_issues("Sutando: I've added that to your calendar.")
    if not issues:
        ok("(ai-b) no access restriction → []")
    else:
        fail("(ai-b) no access restriction → []", f"got {issues}")


def test_access_issues_not_authorized():
    issues = scl.detect_access_issues("Sutando: That isn't authorized for this caller.")
    if _has_pattern(issues, "access_issue"):
        ok("(ai-c) \"isn't authorized\" → access_issue")
    else:
        fail("(ai-c) \"isn't authorized\" → access_issue", f"got {issues}")


# ── detect_task_timeout ──────────────────────────────────────────────────────

def test_task_timeout_triggered():
    issues = scl.detect_task_timeout("Sutando: Sorry, it seems to be taking a while.")
    if _has_pattern(issues, "task_timeout"):
        ok("(tt-a) taking-a-while phrase → task_timeout")
    else:
        fail("(tt-a) taking-a-while → task_timeout", f"got {issues}")


def test_task_timeout_clean():
    issues = scl.detect_task_timeout("Sutando: Done! I've sent the email.")
    if not issues:
        ok("(tt-b) no timeout phrases → []")
    else:
        fail("(tt-b) no timeout phrases → []", f"got {issues}")


# ── detect_confusion ─────────────────────────────────────────────────────────

def test_confusion_two_signals():
    t = "\n".join([
        "Recipient: Hello? Are you there?",
        "Sutando: Yes, I'm here.",
        "Recipient: What? I don't understand.",
    ])
    issues = scl.detect_confusion(t)
    if _has_pattern(issues, "caller_confusion"):
        ok("(cf-a) 2 confusion signals → caller_confusion")
    else:
        fail("(cf-a) 2 confusion signals → caller_confusion", f"got {issues}")


def test_confusion_one_signal_no_flag():
    t = "Recipient: Hello? Are you there?\nSutando: Yes, I'm here."
    issues = scl.detect_confusion(t)
    if not issues:
        ok("(cf-b) only 1 confusion signal → no flag (threshold=2)")
    else:
        fail("(cf-b) only 1 confusion signal → no flag", f"got {issues}")


# ── detect_fabrication ───────────────────────────────────────────────────────

def test_fabrication_triggered():
    t = "Sutando: Your balance is $1,234."
    issues = scl.detect_fabrication(t)
    if _has_pattern(issues, "potential_fabrication"):
        ok("(fb-a) specific balance claim after Sutando → potential_fabrication")
    else:
        fail("(fb-a) specific balance → potential_fabrication", f"got {issues}")


def test_fabrication_clean():
    issues = scl.detect_fabrication("Sutando: Let me check that for you.")
    if not issues:
        ok("(fb-b) no specific data claim → []")
    else:
        fail("(fb-b) no specific data claim → []", f"got {issues}")


# ── detect_reconnect_leak ────────────────────────────────────────────────────

def test_reconnect_leak_im_back():
    t = "Sutando: I'm back! How can I continue helping you?"
    issues = scl.detect_reconnect_leak(t)
    if _has_pattern(issues, "reconnect_leak"):
        ok("(rl-a) \"I'm back\" from Sutando → reconnect_leak")
    else:
        fail("(rl-a) \"I'm back\" → reconnect_leak", f"got {issues}")


def test_reconnect_leak_caller_says_it():
    # Caller saying "I'm back" should NOT trigger — only Sutando lines matter
    t = "Recipient: I'm back. Let's continue.\nSutando: Of course."
    issues = scl.detect_reconnect_leak(t)
    if not issues:
        ok("(rl-b) caller says \"I'm back\" (not Sutando) → no flag")
    else:
        fail("(rl-b) caller says I'm back → no flag", f"got {issues}")


# ── detect_repeated_command ──────────────────────────────────────────────────

def test_repeated_summon_three_times():
    t = "\n".join([
        "Recipient: Can you summon?",
        "Sutando: I'll try.",
        "Recipient: Please summon the screen.",
        "Sutando: Trying again.",
        "Recipient: Summon, share screen!",
    ])
    issues = scl.detect_repeated_command(t)
    if _has_pattern(issues, "repeated_summon"):
        ok("(rc-a) summon 3x → repeated_summon")
    else:
        fail("(rc-a) summon 3x → repeated_summon", f"got {issues}")


def test_repeated_command_clean():
    t = "Recipient: Can you schedule a meeting?\nSutando: Sure."
    issues = scl.detect_repeated_command(t)
    if not issues:
        ok("(rc-b) single command → no flag")
    else:
        fail("(rc-b) single command → no flag", f"got {issues}")


# ── detect_identity_confusion ────────────────────────────────────────────────

def test_identity_confusion_claims_chi():
    t = "Sutando: I'm Chi, your assistant."
    issues = scl.detect_identity_confusion(t)
    if _has_pattern(issues, "identity_confusion"):
        ok("(ic-a) agent claims to be Chi → identity_confusion")
    else:
        fail("(ic-a) claims Chi → identity_confusion", f"got {issues}")


def test_identity_confusion_claims_human():
    t = "Sutando: I am a human, not a robot."
    issues = scl.detect_identity_confusion(t)
    if _has_pattern(issues, "identity_confusion"):
        ok("(ic-b) agent claims to be human → identity_confusion")
    else:
        fail("(ic-b) claims human → identity_confusion", f"got {issues}")


def test_identity_confusion_clean():
    t = "Sutando: I'm Sutando, your AI assistant."
    issues = scl.detect_identity_confusion(t)
    if not issues:
        ok("(ic-c) correct identity → no flag")
    else:
        fail("(ic-c) correct identity → no flag", f"got {issues}")


# ── detect_recording_confusion ───────────────────────────────────────────────

def test_recording_confusion_triggered():
    t = "Recipient: You haven't started recording yet.\nSutando: Let me try again."
    issues = scl.detect_recording_confusion(t)
    if _has_pattern(issues, "recording_confusion"):
        ok("(re-a) recording complaint → recording_confusion")
    else:
        fail("(re-a) recording complaint → recording_confusion", f"got {issues}")


def test_recording_confusion_clean():
    t = "Recipient: Great, please record this meeting.\nSutando: Recording now."
    issues = scl.detect_recording_confusion(t)
    if not issues:
        ok("(re-b) no complaint → []")
    else:
        fail("(re-b) no complaint → []", f"got {issues}")


# ── detect_scroll_frustration ────────────────────────────────────────────────

def test_scroll_frustration_three_requests():
    t = "\n".join([
        "Recipient: Scroll down please.",
        "Sutando: Scrolling.",
        "Recipient: Scroll down more.",
        "Sutando: Done.",
        "Recipient: Can you scroll down again?",
    ])
    issues = scl.detect_scroll_frustration(t)
    if _has_pattern(issues, "scroll_frustration"):
        ok("(sf-a) scroll 3x → scroll_frustration")
    else:
        fail("(sf-a) scroll 3x → scroll_frustration", f"got {issues}")


def test_scroll_frustration_clean():
    t = "Recipient: Scroll down.\nSutando: Done."
    issues = scl.detect_scroll_frustration(t)
    if not issues:
        ok("(sf-b) scroll once → no flag (threshold=3)")
    else:
        fail("(sf-b) scroll once → no flag", f"got {issues}")


# ── detect_metadata_issues ───────────────────────────────────────────────────

def test_metadata_short_call():
    entry = {"duration_seconds": 5, "is_meeting": False}
    issues = scl.detect_metadata_issues(entry)
    if _has_pattern(issues, "short_call"):
        ok("(mi-a) 5s non-meeting call → short_call")
    else:
        fail("(mi-a) 5s call → short_call", f"got {issues}")


def test_metadata_short_meeting_not_flagged():
    # Short meeting durations are expected — not a bug
    entry = {"duration_seconds": 5, "is_meeting": True}
    issues = scl.detect_metadata_issues(entry)
    if not issues:
        ok("(mi-b) 5s meeting → no short_call flag")
    else:
        fail("(mi-b) 5s meeting → no flag", f"got {issues}")


def test_metadata_normal_duration():
    entry = {"duration_seconds": 120, "is_meeting": False}
    issues = scl.detect_metadata_issues(entry)
    if not issues:
        ok("(mi-c) 120s call → no flag")
    else:
        fail("(mi-c) 120s call → no flag", f"got {issues}")


# ── scan_entry ────────────────────────────────────────────────────────────────

def test_scan_entry_clean():
    entry = {"callSid": "CA123", "transcript": "Sutando: Hello.\nCaller: Thanks, bye."}
    result = scl.scan_entry(entry)
    if result is None:
        ok("(se-a) clean entry → scan_entry returns None")
    else:
        fail("(se-a) clean entry → None", f"got {result}")


def test_scan_entry_flagged():
    entry = {
        "callSid": "CA456",
        "transcript": "Sutando: I'm back! Let me continue.\nCaller: What?",
        "duration_seconds": 8,
    }
    result = scl.scan_entry(entry)
    if result is not None and result["callSid"] == "CA456" and result["issue_count"] >= 1:
        ok("(se-b) flagged entry → dict with callSid and issue_count ≥ 1")
    else:
        fail("(se-b) flagged entry → dict", f"got {result}")


def test_scan_entry_short_transcript_skips_detectors():
    # Transcript < 20 chars — metadata-only scan
    entry = {"callSid": "CA789", "transcript": "Hi.", "duration_seconds": 3}
    result = scl.scan_entry(entry)
    # Short call should still flag from metadata even with tiny transcript
    if result is not None and _has_pattern(result["issues"], "short_call"):
        ok("(se-c) short transcript → metadata detectors still run")
    else:
        fail("(se-c) short transcript → metadata detectors still run", f"got {result}")


if __name__ == "__main__":
    print("scan-call-logs tests")
    print("=" * 40)
    test_duplicate_responses_triggered()
    test_duplicate_responses_no_flag_for_distinct_turns()
    test_access_issues_cant_access()
    test_access_issues_clean()
    test_access_issues_not_authorized()
    test_task_timeout_triggered()
    test_task_timeout_clean()
    test_confusion_two_signals()
    test_confusion_one_signal_no_flag()
    test_fabrication_triggered()
    test_fabrication_clean()
    test_reconnect_leak_im_back()
    test_reconnect_leak_caller_says_it()
    test_repeated_summon_three_times()
    test_repeated_command_clean()
    test_identity_confusion_claims_chi()
    test_identity_confusion_claims_human()
    test_identity_confusion_clean()
    test_recording_confusion_triggered()
    test_recording_confusion_clean()
    test_scroll_frustration_three_requests()
    test_scroll_frustration_clean()
    test_metadata_short_call()
    test_metadata_short_meeting_not_flagged()
    test_metadata_normal_duration()
    test_scan_entry_clean()
    test_scan_entry_flagged()
    test_scan_entry_short_transcript_skips_detectors()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)

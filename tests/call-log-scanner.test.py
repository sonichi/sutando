#!/usr/bin/env python3
"""
Tests for the call log scanner bug detection patterns.
Run: python3 tests/call-log-scanner.test.py
"""

import re
import sys

# Inline the detection functions (same as src/scan-call-logs.py) so test is self-contained
class scanner:
    @staticmethod
    def detect_repeated_responses(transcript):
        issues = []
        lines = [l.strip() for l in transcript.split('\n') if l.strip()]
        for i in range(1, len(lines)):
            if lines[i] == lines[i-1] and lines[i].startswith('Sutando:'):
                text = lines[i][len('Sutando:'):].strip()[:80]
                issues.append({"type": "duplicate_response", "text": text})
        return issues

    @staticmethod
    def detect_capability_failures(transcript):
        issues = []
        patterns = [
            (r"I (?:can't|cannot|don't have|am not able to) access", "access_denied"),
            (r"I (?:can't|cannot) (?:help with|do) that", "capability_refused"),
            (r"due to (?:privacy|security) (?:protocols|reasons)", "false_security_block"),
        ]
        for pattern, ptype in patterns:
            matches = re.findall(pattern, transcript, re.IGNORECASE)
            if matches:
                issues.append({"type": ptype, "count": len(matches)})
        return issues

    @staticmethod
    def detect_caller_confusion(transcript):
        issues = []
        patterns = [
            (r"(?:hello|hi)\??\s*(?:are you (?:still )?there|can you hear)", "caller_waiting"),
            (r"(?:it's|that's) taking (?:a |too )?long", "caller_frustrated"),
        ]
        for pattern, ptype in patterns:
            matches = re.findall(pattern, transcript, re.IGNORECASE)
            if matches:
                issues.append({"type": ptype, "count": len(matches)})
        return issues

    @staticmethod
    def scan_call(call):
        transcript = call.get("transcript", "")
        issues = []
        issues.extend(scanner.detect_repeated_responses(transcript))
        issues.extend(scanner.detect_capability_failures(transcript))
        issues.extend(scanner.detect_caller_confusion(transcript))
        return issues

passed = 0
failed = 0

def assert_eq(actual, expected, msg):
    global passed, failed
    if actual == expected:
        passed += 1; print(f"  ✓ {msg}")
    else:
        failed += 1; print(f"  ✗ {msg} (got {actual!r})")

def assert_true(condition, msg):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✓ {msg}")
    else:
        failed += 1; print(f"  ✗ {msg}")

print("\n=== Call Log Scanner Tests ===\n")

# --- Duplicate detection ---
print("Duplicate response detection:")
assert_eq(len(scanner.detect_repeated_responses(
    "Sutando: Hello\nCaller: Hi\nSutando: I'm working on it.\nSutando: I'm working on it."
)), 1, "detects single duplicate")

assert_eq(len(scanner.detect_repeated_responses(
    "Sutando: A\nSutando: A\nSutando: A"
)), 2, "detects triple repeat as 2 duplicates")

assert_eq(len(scanner.detect_repeated_responses(
    "Sutando: Hello\nCaller: Hi\nSutando: Goodbye"
)), 0, "no false positive on normal conversation")

assert_eq(len(scanner.detect_repeated_responses(
    "Caller: Hello\nCaller: Hello"
)), 0, "ignores caller duplicates (not a bug)")

assert_eq(len(scanner.detect_repeated_responses("")), 0, "handles empty transcript")

# --- Capability failure detection ---
print("\nCapability failure detection:")
assert_true(len(scanner.detect_capability_failures(
    "Sutando: I can't access your files or downloads folder."
)) > 0, "detects access denied")

assert_true(len(scanner.detect_capability_failures(
    "Sutando: I cannot help with that request."
)) > 0, "detects capability refused")

assert_true(len(scanner.detect_capability_failures(
    "Sutando: due to privacy protocols, I cannot do that."
)) > 0, "detects false security block")

assert_eq(len(scanner.detect_capability_failures(
    "Sutando: Sure, I found the file."
)), 0, "no false positive on normal response")

# --- Caller confusion detection ---
print("\nCaller confusion detection:")
assert_true(len(scanner.detect_caller_confusion(
    "Caller: Hello? Are you still there?"
)) > 0, "detects caller waiting")

assert_true(len(scanner.detect_caller_confusion(
    "Caller: It's taking too long."
)) > 0, "detects caller frustrated")

assert_eq(len(scanner.detect_caller_confusion(
    "Caller: Thanks, that's great!"
)), 0, "no false positive on happy caller")

# --- Full scan ---
print("\nFull call scan:")
call_with_issues = {"transcript": "Sutando: Working on it.\nSutando: Working on it.\nCaller: Hello? Are you there?"}
issues = scanner.scan_call(call_with_issues)
assert_true(len(issues) >= 2, f"detects multiple issue types ({len(issues)} found)")

clean_call = {"transcript": "Sutando: Hi!\nCaller: What time is it?\nSutando: It's 3pm."}
assert_eq(len(scanner.scan_call(clean_call)), 0, "clean call has no issues")

# --- Reconnect leak detection ---
print("\nReconnect leak detection:")

# Import actual detectors from scanner
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / 'src'))
from importlib import import_module
scan_mod = import_module('scan-call-logs')

assert_true(len(scan_mod.detect_reconnect_leak(
    "Sutando: Hi\nCaller: Hello\nSutando: I'm back."
)) > 0, "detects 'I'm back' reconnect leak")

assert_eq(len(scan_mod.detect_reconnect_leak(
    "Sutando: Hi\nCaller: Hello\nSutando: Sure, I'll check."
)), 0, "no false positive on normal conversation")

# --- Repeated command detection ---
print("\nRepeated command detection:")

assert_true(len(scan_mod.detect_repeated_command(
    "Recipient: summon computer to zoom\nSutando: Sure\nRecipient: summon screen\nSutando: ok\nRecipient: share screen to zoom"
)) > 0, "detects 3x summon attempts")

assert_eq(len(scan_mod.detect_repeated_command(
    "Recipient: summon computer to zoom\nSutando: Summoning now."
)), 0, "single summon is not flagged")

assert_true(len(scan_mod.detect_repeated_command(
    "Recipient: switch to tab\nRecipient: open the tab\nRecipient: switch tab please"
)) > 0, "detects 3x tab switch attempts")

assert_eq(len(scan_mod.detect_repeated_command(
    "Recipient: switch to GitHub tab\nSutando: Switched to GitHub."
)), 0, "single tab switch not flagged")

print(f"\n=== Results: {passed} passed, {failed} failed ===\n")
sys.exit(1 if failed > 0 else 0)

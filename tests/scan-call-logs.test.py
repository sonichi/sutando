#!/usr/bin/env python3
"""Tests for src/scan-call-logs.py — pure detection functions and scan_entry."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "src" / "scan-call-logs.py"

spec = importlib.util.spec_from_file_location("scl", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(REPO_ROOT / "src"))
spec.loader.exec_module(mod)

PASS = 0
FAIL = 0


def ok(label):
    global PASS; PASS += 1; print(f"PASS  {label}")


def fail(label, detail=""):
    global FAIL; FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# T1 — script exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: scan-call-logs.py exists")
else:
    fail("T1: scan-call-logs.py exists")

# ---------------------------------------------------------------------------
# T2 — script has module docstring
# ---------------------------------------------------------------------------
content = SCRIPT.read_text()
body = "\n".join(l for l in content.splitlines() if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T2: script has module docstring")
else:
    fail("T2: script has module docstring")

# ---------------------------------------------------------------------------
# T3 — detect_duplicate_responses() → [] for clean transcript
# ---------------------------------------------------------------------------
clean = "User: hello\nSutando: Hi there!\nUser: thanks\nSutando: You're welcome!"
if mod.detect_duplicate_responses(clean) == []:
    ok("T3: detect_duplicate_responses() → [] for clean transcript")
else:
    fail("T3: [] for clean", f"got={mod.detect_duplicate_responses(clean)}")

# ---------------------------------------------------------------------------
# T4 — detect_duplicate_responses() → issue for consecutive repeated Sutando turns
# ---------------------------------------------------------------------------
repeated_line = "Sutando: I'll take care of that right away for you now."
duped = f"User: hello\n{repeated_line}\n{repeated_line}\n"
result = mod.detect_duplicate_responses(duped)
if result and result[0]["pattern"] == "duplicate_response":
    ok("T4: detect_duplicate_responses() flags consecutive repeated Sutando response")
else:
    fail("T4: flags consecutive duplicate", f"got={result}")

# ---------------------------------------------------------------------------
# T5 — detect_access_issues() → [] for clean transcript
# ---------------------------------------------------------------------------
if mod.detect_access_issues("User: hello\nSutando: Sure I can help!") == []:
    ok("T5: detect_access_issues() → [] for clean transcript")
else:
    fail("T5: [] for clean")

# ---------------------------------------------------------------------------
# T6 — detect_access_issues() → issue when "cannot access" present
# ---------------------------------------------------------------------------
access_t = "Sutando: I cannot access that information for you right now."
result = mod.detect_access_issues(access_t)
if result and "access_issue" in result[0]["pattern"]:
    ok("T6: detect_access_issues() flags 'cannot access' pattern")
else:
    fail("T6: flags access denied", f"got={result}")

# ---------------------------------------------------------------------------
# T7 — detect_reconnect_leak() → [] for clean transcript
# ---------------------------------------------------------------------------
if mod.detect_reconnect_leak("Sutando: Hello! How can I help you today?") == []:
    ok("T7: detect_reconnect_leak() → [] for clean transcript")
else:
    fail("T7: [] for clean")

# ---------------------------------------------------------------------------
# T8 — detect_reconnect_leak() → issue when Sutando says "I'm back"
# ---------------------------------------------------------------------------
reconnect_t = "Sutando: I'm back! How can I help you?"
result = mod.detect_reconnect_leak(reconnect_t)
if result and result[0]["pattern"] == "reconnect_leak":
    ok("T8: detect_reconnect_leak() flags \"I'm back\" in Sutando turn")
else:
    fail("T8: flags reconnect leak", f"got={result}")

# ---------------------------------------------------------------------------
# T9 — detect_identity_confusion() → issue when Sutando claims to be Chi
# ---------------------------------------------------------------------------
identity_t = "User: who are you?\nSutando: I'm Chi, the AI researcher."
result = mod.detect_identity_confusion(identity_t)
if result and "identity_confusion" in result[0]["pattern"]:
    ok("T9: detect_identity_confusion() flags Sutando claiming owner identity")
else:
    fail("T9: flags identity confusion", f"got={result}")

# ---------------------------------------------------------------------------
# T10 — detect_metadata_issues() → [] for normal duration call
# ---------------------------------------------------------------------------
normal_entry = {"duration_seconds": 180, "is_meeting": False}
if mod.detect_metadata_issues(normal_entry) == []:
    ok("T10: detect_metadata_issues() → [] for normal duration (180s)")
else:
    fail("T10: [] for normal call", f"got={mod.detect_metadata_issues(normal_entry)}")

# ---------------------------------------------------------------------------
# T11 — detect_metadata_issues() → issue for very short call (<10s, non-meeting)
# ---------------------------------------------------------------------------
short_entry = {"duration_seconds": 5, "is_meeting": False}
result = mod.detect_metadata_issues(short_entry)
if result and result[0]["pattern"] == "short_call":
    ok("T11: detect_metadata_issues() flags very short call (<10s, non-meeting)")
else:
    fail("T11: flags short call", f"got={result}")

# ---------------------------------------------------------------------------
# T12 — detect_metadata_issues() → [] for short meeting (meetings are expected)
# ---------------------------------------------------------------------------
meeting_entry = {"duration_seconds": 5, "is_meeting": True}
if mod.detect_metadata_issues(meeting_entry) == []:
    ok("T12: detect_metadata_issues() → [] for short meeting (is_meeting=True exempted)")
else:
    fail("T12: meeting exempted from short_call", f"got={mod.detect_metadata_issues(meeting_entry)}")

# ---------------------------------------------------------------------------
# T13 — scan_entry() returns None for clean entry (no issues)
# ---------------------------------------------------------------------------
clean_entry = {
    "callSid": "CA123",
    "timestamp": "2026-05-25T08:00:00Z",
    "duration_seconds": 120,
    "transcript": "User: hello\nSutando: Hi! How can I help?",
}
if mod.scan_entry(clean_entry) is None:
    ok("T13: scan_entry() returns None for clean entry")
else:
    fail("T13: None for clean entry", f"got={mod.scan_entry(clean_entry)}")

# ---------------------------------------------------------------------------
# T14 — scan_entry() returns issue dict with max_severity for flagged entry
# ---------------------------------------------------------------------------
flagged_entry = {
    "callSid": "CA456",
    "timestamp": "2026-05-25T09:00:00Z",
    "duration_seconds": 3,  # triggers short_call (high severity? no, medium)
    "transcript": "",
}
result = mod.scan_entry(flagged_entry)
if result and "issues" in result and result["issue_count"] >= 1 and "max_severity" in result:
    ok(f"T14: scan_entry() returns issue dict with max_severity='{result['max_severity']}'")
else:
    fail("T14: issue dict with max_severity", f"got={result}")

# ---------------------------------------------------------------------------
# T15 — detect_repeated_command() → issue when summon requested 3+ times
# ---------------------------------------------------------------------------
summon_t = "\n".join([
    "Recipient: can you summon",
    "Sutando: Sure, summoning now.",
    "Recipient: summon please",
    "Sutando: On it.",
    "Recipient: please summon again",
    "Sutando: Summoning.",
])
result = mod.detect_repeated_command(summon_t)
if result and result[0]["pattern"] == "repeated_summon":
    ok("T15: detect_repeated_command() flags summon requested 3+ times")
else:
    fail("T15: flags repeated summon", f"got={result}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass

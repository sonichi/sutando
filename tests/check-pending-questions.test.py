#!/usr/bin/env python3
"""
Tests for src/check-pending-questions.py

Covers the pure / file-based functions:
  get_waiting_questions()   — section parser (regex-split impl)
  presenter_mode_active()   — sentinel file check
  should_notify()           — cooldown gate
  voice_client_connected()  — voice-agent.log reader

Subprocess/macOS notification calls (notify_macos, notify_voice,
notify_discord_dm) are not tested here — they require live subsystems.

Run: python3 tests/check-pending-questions.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_pending_questions", REPO / "src" / "check-pending-questions.py"
)
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)

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


# ── get_waiting_questions ────────────────────────────────────────────────────

def _run_gwq(content: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        pq = ws / "pending-questions.md"
        pq.write_text(content)
        with patch.object(cpq, "PQ_FILE", pq):
            return cpq.get_waiting_questions()


def test_gwq_empty_file():
    result = _run_gwq("")
    if result == []:
        ok("(gwq-a) empty file → []")
    else:
        fail("(gwq-a) empty file → []", f"got {result}")


def test_gwq_unanswered_found():
    content = """## Deploy blocker

- **Status:** unanswered
"""
    result = _run_gwq(content)
    if len(result) == 1 and "Deploy blocker" in result[0]["title"]:
        ok("(gwq-b) unanswered section → found")
    else:
        fail("(gwq-b) unanswered section → found", f"got {result}")


def test_gwq_resolved_skipped():
    content = """## Old question

- **Status:** resolved
"""
    result = _run_gwq(content)
    if result == []:
        ok("(gwq-c) resolved → []")
    else:
        fail("(gwq-c) resolved → []", f"got {result}")


def test_gwq_waiting_status():
    content = """## Waiting on vendor

- **Status:** Waiting for reply
"""
    result = _run_gwq(content)
    if len(result) == 1:
        ok("(gwq-d) 'Waiting' status → found")
    else:
        fail("(gwq-d) 'Waiting' status → found", f"got {result}")


def test_gwq_no_status_field():
    content = """## Section without status

Just some text, no Status line.
"""
    result = _run_gwq(content)
    if result == []:
        ok("(gwq-e) no Status field → []")
    else:
        fail("(gwq-e) no Status field → []", f"got {result}")


def test_gwq_multiple_sections_filtered():
    content = """## Resolved thing

- **Status:** resolved

## Open question

- **Status:** unanswered

## Another resolved

- **Status:** resolved
"""
    result = _run_gwq(content)
    if len(result) == 1 and "Open question" in result[0]["title"]:
        ok("(gwq-f) mixed sections → only unanswered returned")
    else:
        fail("(gwq-f) mixed sections → only unanswered returned", f"got {result}")


def test_gwq_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        with patch.object(cpq, "PQ_FILE", ws / "pending-questions.md"):
            result = cpq.get_waiting_questions()
    if result == []:
        ok("(gwq-g) missing file → []")
    else:
        fail("(gwq-g) missing file → []", f"got {result}")


def test_gwq_title_truncated_to_40():
    long_title = "A" * 80
    content = f"## {long_title}\n\n- **Status:** unanswered\n"
    result = _run_gwq(content)
    if len(result) == 1 and len(result[0]["id"]) == 40:
        ok("(gwq-h) long title truncated to 40 chars in id")
    else:
        fail("(gwq-h) long title truncated to 40 chars in id", f"got id={result[0]['id']!r} len={len(result[0]['id']) if result else 'N/A'}")


# ── presenter_mode_active ────────────────────────────────────────────────────

def test_presenter_no_sentinel():
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "presenter-mode.sentinel"
        with patch.object(cpq, "PRESENTER_SENTINEL", sentinel):
            result = cpq.presenter_mode_active()
    if result is False:
        ok("(pm-a) no sentinel → False")
    else:
        fail("(pm-a) no sentinel → False", f"got {result}")


def test_presenter_future_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "presenter-mode.sentinel"
        future = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + 3600))
        sentinel.write_text(future)
        with patch.object(cpq, "PRESENTER_SENTINEL", sentinel):
            result = cpq.presenter_mode_active()
    if result is True:
        ok("(pm-b) future expiry → True (active)")
    else:
        fail("(pm-b) future expiry → True (active)", f"got {result}")


def test_presenter_past_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "presenter-mode.sentinel"
        past = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 3600))
        sentinel.write_text(past)
        with patch.object(cpq, "PRESENTER_SENTINEL", sentinel):
            result = cpq.presenter_mode_active()
    if result is False:
        ok("(pm-c) past expiry → False (inactive)")
    else:
        fail("(pm-c) past expiry → False (inactive)", f"got {result}")


def test_presenter_malformed_content():
    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "presenter-mode.sentinel"
        sentinel.write_text("garbage-not-a-date")
        with patch.object(cpq, "PRESENTER_SENTINEL", sentinel):
            result = cpq.presenter_mode_active()
    if result is False:
        ok("(pm-d) malformed sentinel → False (safe default)")
    else:
        fail("(pm-d) malformed sentinel → False (safe default)", f"got {result}")


# ── should_notify ────────────────────────────────────────────────────────────

def test_should_notify_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        notify_file = Path(tmp) / ".last-pq-notify"
        with patch.object(cpq, "LAST_NOTIFY_FILE", notify_file):
            result = cpq.should_notify()
    if result is True:
        ok("(sn-a) no notify file → True (should notify)")
    else:
        fail("(sn-a) no notify file → True (should notify)", f"got {result}")


def test_should_notify_recent_file():
    with tempfile.TemporaryDirectory() as tmp:
        notify_file = Path(tmp) / ".last-pq-notify"
        notify_file.write_text(str(int(time.time())))
        # mtime = now (within 1h cooldown)
        with patch.object(cpq, "LAST_NOTIFY_FILE", notify_file):
            result = cpq.should_notify()
    if result is False:
        ok("(sn-b) recent notify file → False (cooldown)")
    else:
        fail("(sn-b) recent notify file → False (cooldown)", f"got {result}")


def test_should_notify_old_file():
    with tempfile.TemporaryDirectory() as tmp:
        notify_file = Path(tmp) / ".last-pq-notify"
        notify_file.write_text(str(int(time.time())))
        old_mtime = time.time() - 7200  # 2h ago
        os.utime(notify_file, (old_mtime, old_mtime))
        with patch.object(cpq, "LAST_NOTIFY_FILE", notify_file):
            result = cpq.should_notify()
    if result is True:
        ok("(sn-c) old notify file (2h) → True (cooldown expired)")
    else:
        fail("(sn-c) old notify file (2h) → True (cooldown expired)", f"got {result}")


# ── voice_client_connected ───────────────────────────────────────────────────

def test_voice_no_log():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "voice-agent.log"
        with patch.object(cpq, "VOICE_LOG", log):
            result = cpq.voice_client_connected()
    if result is False:
        ok("(vc-a) no log file → False")
    else:
        fail("(vc-a) no log file → False", f"got {result}")


def test_voice_connected():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "voice-agent.log"
        log.write_text("[2026-05-24] [Health] status=ok client=true sessions=1\n")
        with patch.object(cpq, "VOICE_LOG", log):
            result = cpq.voice_client_connected()
    if result is True:
        ok("(vc-b) log shows client=true → True")
    else:
        fail("(vc-b) log shows client=true → True", f"got {result}")


def test_voice_disconnected():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "voice-agent.log"
        log.write_text("[2026-05-24] [Health] status=ok client=false sessions=0\n")
        with patch.object(cpq, "VOICE_LOG", log):
            result = cpq.voice_client_connected()
    if result is False:
        ok("(vc-c) log shows client=false → False")
    else:
        fail("(vc-c) log shows client=false → False", f"got {result}")


def test_voice_uses_last_health_line():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "voice-agent.log"
        # Older line says connected, newer line says disconnected
        log.write_text(
            "[2026-05-24T10:00] [Health] client=true\n"
            "[2026-05-24T10:01] [Health] client=false\n"
        )
        with patch.object(cpq, "VOICE_LOG", log):
            result = cpq.voice_client_connected()
    if result is False:
        ok("(vc-d) uses last [Health] line — disconnected wins")
    else:
        fail("(vc-d) uses last [Health] line — disconnected wins", f"got {result}")


if __name__ == "__main__":
    print("check-pending-questions tests")
    print("=" * 40)
    test_gwq_empty_file()
    test_gwq_unanswered_found()
    test_gwq_resolved_skipped()
    test_gwq_waiting_status()
    test_gwq_no_status_field()
    test_gwq_multiple_sections_filtered()
    test_gwq_missing_file()
    test_gwq_title_truncated_to_40()
    test_presenter_no_sentinel()
    test_presenter_future_expiry()
    test_presenter_past_expiry()
    test_presenter_malformed_content()
    test_should_notify_no_file()
    test_should_notify_recent_file()
    test_should_notify_old_file()
    test_voice_no_log()
    test_voice_connected()
    test_voice_disconnected()
    test_voice_uses_last_health_line()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)

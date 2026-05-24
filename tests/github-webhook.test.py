#!/usr/bin/env python3
"""
Tests for src/github-webhook.py

Covers the two pure functions (no HTTP server needed):
  verify_github_signature(body, sig_header) → bool
  format_event(event_type, payload)         → str | None

Run: python3 tests/github-webhook.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "github_webhook", REPO / "src" / "github-webhook.py"
)
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)

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


def _make_sig(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ── verify_github_signature ───────────────────────────────────────────────────

def test_verify_no_secret_returns_false():
    body = b'{"action":"opened"}'
    sig = _make_sig("some_secret", body)
    with patch.object(gh, "WEBHOOK_SECRET", ""):
        result = gh.verify_github_signature(body, sig)
    if result is False:
        ok("(vs-a) empty WEBHOOK_SECRET → False (fail-closed)")
    else:
        fail("(vs-a) empty WEBHOOK_SECRET → False", f"got {result!r}")


def test_verify_missing_sig_header_returns_false():
    with patch.object(gh, "WEBHOOK_SECRET", "test_secret"):
        result = gh.verify_github_signature(b"body", "")
    if result is False:
        ok("(vs-b) missing signature header → False")
    else:
        fail("(vs-b) missing signature header → False", f"got {result!r}")


def test_verify_wrong_prefix_returns_false():
    body = b"payload"
    with patch.object(gh, "WEBHOOK_SECRET", "test_secret"):
        result = gh.verify_github_signature(body, "md5=abcdef")
    if result is False:
        ok("(vs-c) wrong hash prefix (md5=) → False")
    else:
        fail("(vs-c) wrong hash prefix → False", f"got {result!r}")


def test_verify_tampered_body_returns_false():
    secret = "correct_secret"
    real_body = b'{"action":"opened"}'
    tampered_body = b'{"action":"deleted"}'
    sig = _make_sig(secret, real_body)
    with patch.object(gh, "WEBHOOK_SECRET", secret):
        result = gh.verify_github_signature(tampered_body, sig)
    if result is False:
        ok("(vs-d) tampered body → False (HMAC mismatch)")
    else:
        fail("(vs-d) tampered body → False", f"got {result!r}")


def test_verify_wrong_secret_returns_false():
    body = b'{"action":"opened"}'
    sig = _make_sig("wrong_secret", body)
    with patch.object(gh, "WEBHOOK_SECRET", "correct_secret"):
        result = gh.verify_github_signature(body, sig)
    if result is False:
        ok("(vs-e) wrong secret → False")
    else:
        fail("(vs-e) wrong secret → False", f"got {result!r}")


def test_verify_valid_signature_returns_true():
    secret = "my_webhook_secret"
    body = b'{"action":"opened","issue":{"number":42}}'
    sig = _make_sig(secret, body)
    with patch.object(gh, "WEBHOOK_SECRET", secret):
        result = gh.verify_github_signature(body, sig)
    if result is True:
        ok("(vs-f) valid HMAC-SHA256 signature → True")
    else:
        fail("(vs-f) valid signature → True", f"got {result!r}")


def test_verify_empty_body_valid_sig_returns_true():
    secret = "s3cr3t"
    body = b""
    sig = _make_sig(secret, body)
    with patch.object(gh, "WEBHOOK_SECRET", secret):
        result = gh.verify_github_signature(body, sig)
    if result is True:
        ok("(vs-g) empty body, valid sig → True")
    else:
        fail("(vs-g) empty body valid sig → True", f"got {result!r}")


# ── format_event ──────────────────────────────────────────────────────────────

def test_format_issue_opened():
    payload = {
        "action": "opened",
        "issue": {"number": 7, "title": "Fix the bug", "body": "Details here"},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "alice"},
    }
    result = gh.format_event("issues", payload)
    if result and "Fix the bug" in result and "#7" in result and "@alice" in result:
        ok("(fe-a) issues/opened → includes title, number, sender")
    else:
        fail("(fe-a) issues/opened → includes title/number/sender", f"got {result!r}")


def test_format_issue_closed_returns_none():
    payload = {
        "action": "closed",
        "issue": {"number": 7, "title": "Fix the bug"},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "alice"},
    }
    result = gh.format_event("issues", payload)
    if result is None:
        ok("(fe-b) issues/closed → None (not handled)")
    else:
        fail("(fe-b) issues/closed → None", f"got {result!r}")


def test_format_pr_opened():
    payload = {
        "action": "opened",
        "pull_request": {"number": 42, "title": "Add tests", "body": "Test coverage"},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "bob"},
    }
    result = gh.format_event("pull_request", payload)
    if result and "Add tests" in result and "#42" in result and "@bob" in result:
        ok("(fe-c) pull_request/opened → includes title, number, sender")
    else:
        fail("(fe-c) pull_request/opened → includes title/number/sender", f"got {result!r}")


def test_format_pr_merged():
    payload = {
        "action": "closed",
        "pull_request": {"number": 99, "title": "Ship it", "body": "", "merged": True},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "carol"},
    }
    result = gh.format_event("pull_request", payload)
    if result and "merged" in result.lower() and "#99" in result:
        ok("(fe-d) pull_request/closed+merged → merge message with PR number")
    else:
        fail("(fe-d) pull_request/closed+merged → merge message", f"got {result!r}")


def test_format_pr_closed_not_merged_returns_none():
    payload = {
        "action": "closed",
        "pull_request": {"number": 5, "title": "Abandoned", "body": "", "merged": False},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "dave"},
    }
    result = gh.format_event("pull_request", payload)
    if result is None:
        ok("(fe-e) pull_request/closed (not merged) → None")
    else:
        fail("(fe-e) PR closed not merged → None", f"got {result!r}")


def test_format_star_created():
    payload = {
        "action": "created",
        "repository": {"full_name": "user/repo", "stargazers_count": 100},
        "sender": {"login": "fanatic"},
    }
    result = gh.format_event("star", payload)
    if result and "@fanatic" in result and "100" in result:
        ok("(fe-f) star/created → message with sender and star count")
    else:
        fail("(fe-f) star/created → sender and count", f"got {result!r}")


def test_format_issue_comment_human():
    payload = {
        "action": "created",
        "issue": {"number": 3, "title": "Some issue"},
        "comment": {"body": "Great work!", "user": {"login": "eve", "type": "User"}},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "eve"},
    }
    result = gh.format_event("issue_comment", payload)
    if result and "Great work!" in result and "#3" in result:
        ok("(fe-g) issue_comment/created (human) → includes comment body and issue number")
    else:
        fail("(fe-g) issue_comment human → body and issue number", f"got {result!r}")


def test_format_issue_comment_bot_returns_none():
    payload = {
        "action": "created",
        "issue": {"number": 3, "title": "Some issue"},
        "comment": {"body": "Automated comment", "user": {"login": "ci-bot", "type": "Bot"}},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "ci-bot"},
    }
    result = gh.format_event("issue_comment", payload)
    if result is None:
        ok("(fe-h) issue_comment Bot user → None (skipped)")
    else:
        fail("(fe-h) issue_comment Bot → None", f"got {result!r}")


def test_format_unknown_event_returns_none():
    payload = {"action": "ping", "repository": {"full_name": "user/repo"}, "sender": {"login": "x"}}
    result = gh.format_event("ping", payload)
    if result is None:
        ok("(fe-i) unknown event type → None")
    else:
        fail("(fe-i) unknown event → None", f"got {result!r}")


def test_format_issue_body_truncated_at_500():
    long_body = "x" * 600
    payload = {
        "action": "opened",
        "issue": {"number": 1, "title": "T", "body": long_body},
        "repository": {"full_name": "user/repo"},
        "sender": {"login": "u"},
    }
    result = gh.format_event("issues", payload)
    # Body truncated to 500 chars — result shouldn't contain the full 600 chars
    if result and "x" * 501 not in result and "x" * 500 in result:
        ok("(fe-j) issue body truncated to 500 chars")
    else:
        fail("(fe-j) issue body truncated to 500", f"result length={len(result) if result else 'None'}")


if __name__ == "__main__":
    print("github-webhook tests")
    print("=" * 40)
    test_verify_no_secret_returns_false()
    test_verify_missing_sig_header_returns_false()
    test_verify_wrong_prefix_returns_false()
    test_verify_tampered_body_returns_false()
    test_verify_wrong_secret_returns_false()
    test_verify_valid_signature_returns_true()
    test_verify_empty_body_valid_sig_returns_true()
    test_format_issue_opened()
    test_format_issue_closed_returns_none()
    test_format_pr_opened()
    test_format_pr_merged()
    test_format_pr_closed_not_merged_returns_none()
    test_format_star_created()
    test_format_issue_comment_human()
    test_format_issue_comment_bot_returns_none()
    test_format_unknown_event_returns_none()
    test_format_issue_body_truncated_at_500()
    print("=" * 40)
    print(f"Results: {PASS} passed, {FAIL} failed")
    sys.exit(0 if FAIL == 0 else 1)

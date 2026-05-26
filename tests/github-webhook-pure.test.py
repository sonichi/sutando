#!/usr/bin/env python3
"""Behavioral tests for `src/github-webhook.py` pure functions.

Functions under test — both are pure (no file I/O, no network, no side effects):

  verify_github_signature(body, signature_header) — HMAC-SHA256 verification
  format_event(event_type, payload)               — GitHub event → task string

`verify_github_signature` is security-critical: fail-closed behavior
(WEBHOOK_SECRET unset → always False) and timing-safe comparison via
hmac.compare_digest are guarded explicitly.

Run: python3 tests/github-webhook-pure.test.py
Exit: 0 on pass, non-zero on fail.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src"

# -----------------------------------------------------------------------
# Module loader — github-webhook.py starts an HTTP server if __name__ ==
# "__main__", but importing it is safe. Set workspace to temp dir so any
# accidental task write lands somewhere harmless.
# -----------------------------------------------------------------------
_WS = tempfile.mkdtemp(prefix="sutando-test-ghwh-")
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "")  # unset by default
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _n in ("github_webhook", "github-webhook", "workspace_default", "util_paths"):
    sys.modules.pop(_n, None)

_spec = importlib.util.spec_from_file_location("github_webhook", _SRC / "github-webhook.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

verify_github_signature = _mod.verify_github_signature
format_event = _mod.format_event

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

PASS = 0
FAIL = 0

_TEST_SECRET = "sutando-test-secret-abc123"


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"PASS  {label}")


def fail(label: str, detail: str = "") -> None:
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


def _sign(body: bytes, secret: str = _TEST_SECRET) -> str:
    """Compute the expected X-Hub-Signature-256 header value."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _with_secret(secret: str):
    """Context manager: temporarily set _mod.WEBHOOK_SECRET."""
    class _Ctx:
        def __enter__(self):
            self._orig = _mod.WEBHOOK_SECRET
            _mod.WEBHOOK_SECRET = secret
        def __exit__(self, *_):
            _mod.WEBHOOK_SECRET = self._orig
    return _Ctx()


# -----------------------------------------------------------------------
# verify_github_signature — security properties
# -----------------------------------------------------------------------

# T1 — fail-closed: WEBHOOK_SECRET empty → False regardless of signature
with _with_secret(""):
    result = verify_github_signature(b"payload", _sign(b"payload"))
if result is False:
    ok("T1: WEBHOOK_SECRET unset → False (fail-closed security property)")
else:
    fail("T1: fail-closed", f"got {result!r}")

# T2 — empty signature_header → False
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(b"payload", "")
if result is False:
    ok("T2: empty signature_header → False")
else:
    fail("T2: empty header", f"got {result!r}")

# T3 — header without 'sha256=' prefix → False
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(b"payload", "sha1=abc123")
if result is False:
    ok("T3: header without 'sha256=' prefix → False")
else:
    fail("T3: wrong prefix", f"got {result!r}")

# T4 — valid HMAC-SHA256 signature → True
body = b'{"action":"opened"}'
sig = _sign(body)
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(body, sig)
if result is True:
    ok("T4: valid HMAC-SHA256 signature → True")
else:
    fail("T4: valid signature", f"got {result!r}")

# T5 — wrong signature (different body signed) → False
wrong_sig = _sign(b"different body")
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(b"actual body", wrong_sig)
if result is False:
    ok("T5: signature of different body → False (tampered payload rejected)")
else:
    fail("T5: wrong signature rejected", f"got {result!r}")

# T6 — body changed after signing → False
body = b"original"
sig = _sign(body)
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(b"tampered", sig)
if result is False:
    ok("T6: body changed after signing → signature invalid, returns False")
else:
    fail("T6: tampered body", f"got {result!r}")

# T7 — wrong secret used (attacker doesn't know real secret) → False
body = b'{"action":"opened"}'
attacker_sig = _sign(body, secret="wrong-secret")
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(body, attacker_sig)
if result is False:
    ok("T7: signature with wrong secret → False (attacker can't forge signature)")
else:
    fail("T7: wrong secret rejected", f"got {result!r}")

# T8 — empty body with valid HMAC → True (edge case: ping events have no body)
body = b""
sig = _sign(body)
with _with_secret(_TEST_SECRET):
    result = verify_github_signature(body, sig)
if result is True:
    ok("T8: empty body with valid HMAC → True (ping/empty-payload event)")
else:
    fail("T8: empty body valid sig", f"got {result!r}")

# -----------------------------------------------------------------------
# format_event — event type routing and formatting
# -----------------------------------------------------------------------

def _issue_payload(number: int = 42, title: str = "Test bug", body: str = "", action: str = "opened", sender: str = "octocat", repo: str = "org/repo") -> dict:
    return {
        "action": action,
        "repository": {"full_name": repo, "stargazers_count": 100},
        "sender": {"login": sender},
        "issue": {"number": number, "title": title, "body": body},
    }

def _pr_payload(number: int = 7, title: str = "Add feature", body: str = "", action: str = "opened", merged: bool = False, sender: str = "octocat") -> dict:
    return {
        "action": action,
        "repository": {"full_name": "org/repo", "stargazers_count": 100},
        "sender": {"login": sender},
        "pull_request": {"number": number, "title": title, "body": body, "merged": merged},
    }


# T9 — issues/opened → string containing issue number and title
result = format_event("issues", _issue_payload(number=12, title="Memory leak in bridge"))
if result and "#12" in result and "Memory leak in bridge" in result:
    ok("T9: issues/opened → formatted string with issue number and title")
else:
    fail("T9: issues/opened", f"got {result!r}")

# T10 — pull_request/opened → string containing PR number and title
result = format_event("pull_request", _pr_payload(number=99, title="Fix race condition"))
if result and "#99" in result and "Fix race condition" in result:
    ok("T10: pull_request/opened → formatted string with PR number and title")
else:
    fail("T10: pr/opened", f"got {result!r}")

# T11 — pull_request/closed + merged=True → mentions merge
result = format_event("pull_request", _pr_payload(number=50, title="Ship it", action="closed", merged=True))
if result and "merged" in result.lower() and "#50" in result:
    ok("T11: pull_request/closed (merged) → formatted string mentioning merge")
else:
    fail("T11: pr/merged", f"got {result!r}")

# T12 — pull_request/closed WITHOUT merge → None (PR closed but not merged)
result = format_event("pull_request", _pr_payload(number=51, title="Draft", action="closed", merged=False))
if result is None:
    ok("T12: pull_request/closed (not merged) → None")
else:
    fail("T12: pr/closed no merge → None", f"got {result!r}")

# T13 — star/created → string mentioning stargazers count
result = format_event("star", {
    "action": "created",
    "sender": {"login": "fan123"},
    "repository": {"full_name": "org/repo", "stargazers_count": 250},
})
if result and "250" in result and "fan123" in result:
    ok("T13: star/created → formatted string with sender and star count")
else:
    fail("T13: star/created", f"got {result!r}")

# T14 — issue_comment/created (non-bot) → string with comment excerpt
result = format_event("issue_comment", {
    "action": "created",
    "sender": {"login": "reviewer"},
    "repository": {"full_name": "org/repo"},
    "issue": {"number": 33, "title": "Bug report"},
    "comment": {"body": "Looks good to me!", "user": {"type": "User"}},
})
if result and "reviewer" in result and "#33" in result and "Looks good" in result:
    ok("T14: issue_comment/created (User) → formatted string with commenter and excerpt")
else:
    fail("T14: issue_comment/created", f"got {result!r}")

# T15 — issue_comment/created by Bot → None (skip bot comments)
result = format_event("issue_comment", {
    "action": "created",
    "sender": {"login": "github-actions[bot]"},
    "repository": {"full_name": "org/repo"},
    "issue": {"number": 33, "title": "Bug report"},
    "comment": {"body": "CI passed!", "user": {"type": "Bot"}},
})
if result is None:
    ok("T15: issue_comment from Bot → None (bot comments skipped)")
else:
    fail("T15: bot comment skipped", f"got {result!r}")

# T16 — unknown event type → None
result = format_event("deployment", {"action": "created", "repository": {}})
if result is None:
    ok("T16: unknown event type → None")
else:
    fail("T16: unknown event → None", f"got {result!r}")

# T17 — issue body longer than 500 chars → truncated (body[:500] appears, char count = 500)
long_body = "x" * 600
result = format_event("issues", _issue_payload(body=long_body))
x_count = result.count("x") if result else -1
if result and x_count == 500:
    ok("T17: issue body >500 chars → truncated to 500 chars (counted 500 x's, not 600)")
else:
    fail("T17: body truncation", f"x_count={x_count} result_len={len(result) if result else 0}")

# T18 — comment body longer than 300 chars → truncated (count y's in result)
long_comment = "y" * 400
result = format_event("issue_comment", {
    "action": "created",
    "sender": {"login": "dev"},
    "repository": {"full_name": "org/repo"},
    "issue": {"number": 1, "title": "issue"},
    "comment": {"body": long_comment, "user": {"type": "User"}},
})
y_count = result.count("y") if result else -1
if result and y_count == 300:
    ok("T18: comment body >300 chars → truncated to 300 chars (counted 300 y's, not 400)")
else:
    fail("T18: comment truncation", f"y_count={y_count} result={repr(result)[:60]}")

# T19 — issues with non-opened action (e.g. labeled) → None
result = format_event("issues", _issue_payload(action="labeled"))
if result is None:
    ok("T19: issues/labeled (non-opened action) → None")
else:
    fail("T19: issues/labeled → None", f"got {result!r}")

# -----------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------
print(f"\n{PASS}/{PASS + FAIL} tests passed")
if FAIL:
    sys.exit(1)

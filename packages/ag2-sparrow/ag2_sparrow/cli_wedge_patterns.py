"""The abnormal-banner table of the CLI progress detector: text on a pane that says the CLI is parked
(a login prompt, a provider limit, a compaction, an error it sits on), as data.

Extracted from cli_wedge so a reader that only needs the grammar imports no detector: stdlib only,
vendored byte-identical into the ag2-sparrow package by packages/ag2-sparrow/tools/sync_from_src.py.
"""
from __future__ import annotations

import re

# The CLI's line-start forms for a rejected credential. A bare "401" never counts: it
# needs the OAuth-expiry words, an API Error banner, or the CLI's "· Please run /login" tail.
_AUTH_REJECTED = (
    r"(?:API ?Error\s*[:(]?\s*)?401\b.{0,40}?\bOAuth (?:access )?token (?:has )?expired\b.*$"
    r"|OAuth (?:access )?token has expired\b.{0,200}$"
    r"|API ?Error\s*[:(]?\s*401\b.*$"
    r"|API ?Error\b.{0,120}?\bauthentication_error\b.*$"
    r"|Invalid API key\b.{0,80}$"
    r"|[A-Za-z][^\n]{0,600}?\s·\s*(?:please )?run /login\b.{0,40}$"
    r"|(?:please )?run /login\b.{0,40}$"
)

ABNORMAL_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(rx, re.IGNORECASE))
    for name, rx in (
        ("quota-limit", r"(you('ve| have)? )?(hit|reached|exceeded)\b.{0,24}\b(session|usage|weekly|daily|plan) limit\b|(session|usage|weekly|daily|plan) limit (reached|exceeded|hit)\b|(you('ve| have)? )?hit your\b.{0,24}\blimit\b|/?usage-credits\b"),
        ("out-of-credits", r"(you('re| are)? )?out of (usage )?credits?\b|credit balance (is )?(too )?low\b|insufficient credits?\b"),
        ("needs-login", r"(please )?(log ?in|sign ?in) to continue\b|session expired\b|(login|oauth access token has) expired\b.{0,60}/login\b|(you('re| are) )?not logged in\b.{0,60}/login\b|authentication (required|failed)\b|(please )?run /login\b|Select login method\b|Paste code here\b|Browser didn'?t open\b"
                        r"|" + _AUTH_REJECTED),
        ("compacting", r"compact(ing|ion)\b"),
        ("awaiting-input", r"(waiting|awaiting) for (your )?(input|approval|confirmation)\b"),
        # Parked ON an error, which is not a retry: nothing is being attempted.
        ("api-error", r"\bAPI ?Error\b|\b(internal server error|bad gateway|service unavailable)\b|\bHTTP [45]\d\d\b"),
        ("network-error", r"\bnetwork error\b|\bfetch failed\b|\bcould not reach\b|\bE(CONNREFUSED|NOTFOUND|HOSTUNREACH)\b|\bdns (lookup )?failed\b"),
    )
)

# Kept so an existing reader still sees the provider-limit family by name.
PROVIDER_LIMIT_PATTERNS = ("quota-limit", "out-of-credits")

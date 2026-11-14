"""Constant-time equality for a presented credential string against an expected one.

`hmac.compare_digest` on two `str` raises TypeError when either holds a non-ASCII
character, and HTTP headers and decoded paths can carry one. Every request-side
credential check (bearer tokens, webhook signatures, capability paths) compares
through `secret_equal`, which compares UTF-8 bytes and treats a wrong type, an
empty value, or a value that cannot be encoded as a plain mismatch.
"""
from __future__ import annotations

import hmac


def secret_equal(presented: object, expected: object) -> bool:
    if not isinstance(presented, str) or not isinstance(expected, str):
        return False
    if not presented or not expected:
        return False
    try:
        presented_bytes = presented.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(presented_bytes, expected_bytes)

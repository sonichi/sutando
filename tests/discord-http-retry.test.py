#!/usr/bin/env python3
"""Tests for src/channels/discord/http.py request_json() — 429 Retry-After + 5xx backoff.

The Discord reader scripts had no rate-limit handling; a 429 mid-pagination
raised HTTPError and aborted the read (2026-07-24 30-day-history truncation).
request_json() honors Retry-After and retries transient 5xx. These tests inject
a fake urlopen (scripted responses) and a fake sleep (records waits) so backoff
is asserted without wall-clock delay or a live Discord.

Run: python3 tests/discord-http-retry.test.py
"""
from __future__ import annotations

import email.message
import io
import sys
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import channels.discord.http as discord_http  # noqa: E402


def _http_error(code: int, *, retry_after_header=None, body_bytes=b"{}"):
    """Build an HTTPError with optional Retry-After header + readable body."""
    hdrs = email.message.Message()
    if retry_after_header is not None:
        hdrs["Retry-After"] = str(retry_after_header)
    return urllib.error.HTTPError(
        url="https://discord.test/x", code=code, msg="err",
        hdrs=hdrs, fp=io.BytesIO(body_bytes),
    )


class _OkResponse:
    """Context-manager response whose read() returns JSON bytes."""
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _scripted_urlopen(outcomes):
    """Return a fake urlopen that yields each scripted outcome in turn.
    An Exception outcome is raised; anything else is returned."""
    seq = iter(outcomes)

    def fake(req, timeout=None):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    return fake


def _run(outcomes, **kw):
    """Drive request_json with a scripted urlopen + recording sleep."""
    waits: list[float] = []
    orig = urllib.request.urlopen
    urllib.request.urlopen = _scripted_urlopen(outcomes)
    try:
        result = discord_http.request_json(
            urllib.request.Request("https://discord.test/x"),
            sleep=waits.append, **kw,
        )
        return result, waits, None
    except Exception as e:  # noqa: BLE001 - test needs to capture whatever raised
        return None, waits, e
    finally:
        urllib.request.urlopen = orig


def check(cond, label):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    return bool(cond)


def main() -> int:
    print("discord_http.request_json — retry/backoff tests")
    print("=" * 50)
    results = []

    # 1. Success first try → no sleep, returns decoded body.
    r, waits, err = _run([_OkResponse(b'{"ok": true}')])
    results.append(check(err is None and r == {"ok": True} and waits == [],
                         "success first try: decoded body, no sleep"))

    # 2. 429 with Retry-After header → sleeps ~header+cushion, then succeeds.
    r, waits, err = _run([
        _http_error(429, retry_after_header="2"),
        _OkResponse(b'{"ok": 1}'),
    ])
    results.append(check(err is None and r == {"ok": 1} and len(waits) == 1 and 2.0 <= waits[0] <= 3.0,
                         f"429 header: retried after ~2s (got {waits})"))

    # 3. 429 with JSON retry_after body (no header) → uses body value.
    r, waits, err = _run([
        _http_error(429, retry_after_header=None, body_bytes=b'{"retry_after": 1.0}'),
        _OkResponse(b'{"ok": 2}'),
    ])
    results.append(check(err is None and r == {"ok": 2} and len(waits) == 1 and 1.0 <= waits[0] <= 2.0,
                         f"429 body retry_after: used body value (got {waits})"))

    # 4. 5xx with no Retry-After → exponential backoff, then succeeds.
    r, waits, err = _run([
        _http_error(503),
        _http_error(503),
        _OkResponse(b'{"ok": 3}'),
    ])
    results.append(check(err is None and r == {"ok": 3} and len(waits) == 2 and waits[0] < waits[1],
                         f"5xx: exponential backoff increases (got {waits})"))

    # 5. Retries exhausted → re-raises the HTTPError.
    r, waits, err = _run([_http_error(429, retry_after_header="0") for _ in range(4)],
                         max_retries=2)
    results.append(check(isinstance(err, urllib.error.HTTPError) and err.code == 429 and len(waits) == 2,
                         f"exhausts max_retries=2 then raises (slept {len(waits)}x)"))

    # 6. Non-retryable status (403) → raises immediately, no sleep.
    r, waits, err = _run([_http_error(403)])
    results.append(check(isinstance(err, urllib.error.HTTPError) and err.code == 403 and waits == [],
                         "403 non-retryable: raises immediately, no sleep"))

    # 7. Unparseable 429 body + no header → falls back to exponential backoff.
    r, waits, err = _run([
        _http_error(429, retry_after_header=None, body_bytes=b"not json"),
        _OkResponse(b'{"ok": 4}'),
    ])
    results.append(check(err is None and r == {"ok": 4} and len(waits) == 1,
                         "429 unparseable body: falls back to backoff, still retries"))

    # 8. Non-numeric Retry-After header → header parse fails, falls through to
    #    the JSON body's retry_after value.
    r, waits, err = _run([
        _http_error(429, retry_after_header="soon", body_bytes=b'{"retry_after": 1.0}'),
        _OkResponse(b'{"ok": 5}'),
    ])
    results.append(check(err is None and r == {"ok": 5} and len(waits) == 1 and 1.0 <= waits[0] <= 2.0,
                         f"non-numeric header: falls through to body value (got {waits})"))

    # 9. Body retry_after present but non-numeric → both parses fail → backoff.
    r, waits, err = _run([
        _http_error(429, retry_after_header=None, body_bytes=b'{"retry_after": "abc"}'),
        _OkResponse(b'{"ok": 6}'),
    ])
    results.append(check(err is None and r == {"ok": 6} and len(waits) == 1,
                         "non-numeric body retry_after: falls back to backoff"))

    print("=" * 50)
    passed = sum(1 for x in results if x)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

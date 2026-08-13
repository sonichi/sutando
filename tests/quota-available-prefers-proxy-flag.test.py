#!/usr/bin/env python3
"""`read-quota.py` must not contradict the proxy's own `available` flag.

The proxy writes a top-level `available` bool alongside the raw response
headers. `read-quota.py` ignored it and re-derived `available` as
`status == "allowed"`, so any status outside that one literal read as
unavailable. Observed live 2026-08-13: the proxy wrote

    {"available": true, "headers": {"anthropic-ratelimit-unified-status":
                                    "allowed_warning"}, ...}

and `read-quota.py --gate` exited 1 — its documented contract is "exit 1 if
exhausted", while 24% of the 7d pool remained and requests were being served.

`allowed_warning` appears in no docs; `src/health-check.py` still describes the
vocabulary as "allowed" or "rejected". That is why the fix is to trust the
FLAG rather than allowlist the status: an allowlist would break on the next
unlisted value. `test_absent_flag_allowed_warning_still_unavailable` is the
boundary that distinguishes the two designs.

Run: python3 tests/quota-available-prefers-proxy-flag.test.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"


def _load():
    """Extract `resolve_available` from read-quota.py without importing it.

    A plain import is not available: at module scope the script resolves a
    quota-state path and calls `sys.exit(1)` when it is absent, which is the
    normal condition in CI. So locate the function by name and exec just its
    definition. Extracting from the real source (rather than restating the rule
    here) means a rename or reshape fails loudly instead of leaving this suite
    green against a private copy that has drifted from production.
    """
    src = SCRIPT.read_text()
    marker = "def resolve_available("
    if marker not in src:
        raise AssertionError(
            "read-quota.py no longer defines resolve_available() — the "
            "implementation moved. Update this test to match the new shape "
            "rather than leaving it green against code that is gone."
        )
    start = src.index(marker)
    rest = src[start:]
    # The function ends at the next top-level `def `/`if `/`class ` line.
    end = len(rest)
    for i, line in enumerate(rest.split("\n")):
        if i and line and not line[0].isspace():
            end = sum(len(l) + 1 for l in rest.split("\n")[:i])
            break
    ns: dict = {}
    exec(rest[:end], ns)  # noqa: S102 - the extracted production function
    return ns["resolve_available"]


resolve_available = _load()


def _check(status, proxy, expected):
    got = resolve_available(status, proxy)
    assert got is expected, (
        f"status={status!r} proxy_available={proxy!r} -> {got!r}, "
        f"expected {expected!r}"
    )


def test_proxy_true_with_allowed_warning_is_available():
    """The reported defect: returned False, so --gate exited 1 mid-quota."""
    _check("allowed_warning", True, True)


def test_explicit_rejected_wins_over_optimistic_flag():
    """A stale `available: true` must not mask a real rejection."""
    _check("rejected", True, False)


def test_proxy_false_is_authoritative_over_allowed_status():
    _check("allowed", False, False)


def test_absent_flag_falls_back_to_allowed():
    _check("allowed", None, True)


def test_absent_flag_rejected_is_unavailable():
    _check("rejected", None, False)


def test_absent_flag_unknown_stays_unavailable():
    """Deliberately conservative, and deliberately NOT health-check's rule:
    declining to page and declining to proceed have opposite fail-safes."""
    _check("unknown", None, False)


def test_absent_flag_allowed_warning_still_unavailable():
    """The design boundary. Had we allowlisted the status instead of trusting
    the flag, this would be True — and the next unlisted status would regress.
    """
    _check("allowed_warning", None, False)


def test_non_bool_flag_is_not_trusted():
    """A string/None/number is not evidence; fall back rather than coerce."""
    _check("allowed_warning", "true", False)
    _check("allowed_warning", 1, False)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print(f"All {len(tests)} quota-available tests passed.")


if __name__ == "__main__":
    main()

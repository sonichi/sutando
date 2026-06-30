#!/usr/bin/env python3
"""Tests for boundary-check functions in src/discord-read.py.

Covers the two predicate functions that control pagination when --until is set:
  - _at_or_before_boundary(msg): True when msg is AT or OLDER than the boundary
  - _strictly_older_than_boundary(msg): True only when msg is STRICTLY OLDER

These guard the backward-pagination loop — a wrong result either fetches too
many pages (wasted API calls) or truncates the context window (drops messages
the caller needs). They support two --until modes:

  ID mode:    --until is a decimal snowflake string ("1234567890")
  ISO mode:   --until is an ISO datetime prefix ("2026-06-24T23:25")

Run: python3 tests/discord-read-boundary.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# discord-read.py makes a live HTTP call at module level (no __main__ guard).
# Intercept urllib.request.urlopen before exec_module so no real request fires.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-only-token")


def _fake_urlopen(req, timeout=None):
    m = MagicMock()
    m.__enter__ = lambda self: self
    m.__exit__ = MagicMock(return_value=False)
    m.read.return_value = json.dumps([]).encode()
    return m


spec = importlib.util.spec_from_file_location("discord_read", REPO / "src" / "discord-read.py")
dr = importlib.util.module_from_spec(spec)
with patch.object(urllib.request, "urlopen", _fake_urlopen):
    sys.argv = ["discord-read.py", "12345", "--until", "99999"]
    spec.loader.exec_module(dr)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# _at_or_before_boundary — ID (snowflake) mode
# ---------------------------------------------------------------------------

def test_at_boundary_id_equal() -> list[str]:
    """msg["id"] == until → True (at the boundary, include it)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id==until should be True", dr._at_or_before_boundary({"id": "1000"}), fails)
    return fails


def test_at_boundary_id_older() -> list[str]:
    """msg["id"] < until → True (older than boundary)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id<until should be True", dr._at_or_before_boundary({"id": "999"}), fails)
    return fails


def test_at_boundary_id_newer() -> list[str]:
    """msg["id"] > until → False (newer than boundary, not yet reached)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id>until should be False", not dr._at_or_before_boundary({"id": "1001"}), fails)
    return fails


def test_at_boundary_id_missing() -> list[str]:
    """Missing id key → False (no crash, conservative default)."""
    fails: list[str] = []
    dr.args.until = "1000"
    result = dr._at_or_before_boundary({})
    check("missing id should return False", not result, fails)
    return fails


def test_at_boundary_id_non_numeric() -> list[str]:
    """Non-numeric id with numeric --until → False (ValueError caught, no crash)."""
    fails: list[str] = []
    dr.args.until = "1000"
    result = dr._at_or_before_boundary({"id": "not-a-number"})
    check("non-numeric id should return False", not result, fails)
    return fails


# ---------------------------------------------------------------------------
# _at_or_before_boundary — ISO timestamp mode
# ---------------------------------------------------------------------------

def test_at_boundary_iso_equal_prefix() -> list[str]:
    """Timestamp whose prefix equals --until → True (at boundary)."""
    fails: list[str] = []
    dr.args.until = "2026-06-24T23:25"
    msg = {"id": "abc", "timestamp": "2026-06-24T23:25:00.000000+00:00"}
    check("iso at boundary should be True", dr._at_or_before_boundary(msg), fails)
    return fails


def test_at_boundary_iso_older() -> list[str]:
    """Timestamp older than --until prefix → True."""
    fails: list[str] = []
    dr.args.until = "2026-06-24T23:25"
    msg = {"id": "abc", "timestamp": "2026-06-24T20:00:00.000000+00:00"}
    check("iso older than boundary should be True", dr._at_or_before_boundary(msg), fails)
    return fails


def test_at_boundary_iso_newer() -> list[str]:
    """Timestamp newer than --until prefix → False."""
    fails: list[str] = []
    dr.args.until = "2026-06-24T23:25"
    msg = {"id": "abc", "timestamp": "2026-06-25T00:00:00.000000+00:00"}
    check("iso newer than boundary should be False", not dr._at_or_before_boundary(msg), fails)
    return fails


# ---------------------------------------------------------------------------
# _strictly_older_than_boundary — ID mode
# ---------------------------------------------------------------------------

def test_strictly_older_id_equal() -> list[str]:
    """msg["id"] == until → False (at boundary = NOT strictly older)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id==until should be False for strictly-older", not dr._strictly_older_than_boundary({"id": "1000"}), fails)
    return fails


def test_strictly_older_id_older() -> list[str]:
    """msg["id"] < until → True (strictly older)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id<until should be True for strictly-older", dr._strictly_older_than_boundary({"id": "999"}), fails)
    return fails


def test_strictly_older_id_newer() -> list[str]:
    """msg["id"] > until → False (newer than boundary)."""
    fails: list[str] = []
    dr.args.until = "1000"
    check("id>until should be False for strictly-older", not dr._strictly_older_than_boundary({"id": "1001"}), fails)
    return fails


def test_strictly_older_id_missing() -> list[str]:
    """Missing id key with numeric --until → False (no crash)."""
    fails: list[str] = []
    dr.args.until = "1000"
    result = dr._strictly_older_than_boundary({})
    check("missing id should return False", not result, fails)
    return fails


# ---------------------------------------------------------------------------
# _strictly_older_than_boundary — ISO mode
# ---------------------------------------------------------------------------

def test_strictly_older_iso_equal_prefix() -> list[str]:
    """Timestamp at the exact prefix boundary → False (not strictly older)."""
    fails: list[str] = []
    dr.args.until = "2026-06-24T23:25"
    msg = {"id": "abc", "timestamp": "2026-06-24T23:25:00.000000+00:00"}
    check("iso at boundary should be False for strictly-older", not dr._strictly_older_than_boundary(msg), fails)
    return fails


def test_strictly_older_iso_older() -> list[str]:
    """Timestamp clearly older than --until → True."""
    fails: list[str] = []
    dr.args.until = "2026-06-24T23:25"
    msg = {"id": "abc", "timestamp": "2026-06-24T10:00:00.000000+00:00"}
    check("iso older than boundary should be True for strictly-older", dr._strictly_older_than_boundary(msg), fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("at_or_before: id == until → True", test_at_boundary_id_equal),
        ("at_or_before: id < until → True (older)", test_at_boundary_id_older),
        ("at_or_before: id > until → False (newer)", test_at_boundary_id_newer),
        ("at_or_before: missing id → False (no crash)", test_at_boundary_id_missing),
        ("at_or_before: non-numeric id → False (no crash)", test_at_boundary_id_non_numeric),
        ("at_or_before: ISO at prefix boundary → True", test_at_boundary_iso_equal_prefix),
        ("at_or_before: ISO older → True", test_at_boundary_iso_older),
        ("at_or_before: ISO newer → False", test_at_boundary_iso_newer),
        ("strictly_older: id == until → False (not strictly older)", test_strictly_older_id_equal),
        ("strictly_older: id < until → True", test_strictly_older_id_older),
        ("strictly_older: id > until → False", test_strictly_older_id_newer),
        ("strictly_older: missing id → False (no crash)", test_strictly_older_id_missing),
        ("strictly_older: ISO at prefix → False", test_strictly_older_iso_equal_prefix),
        ("strictly_older: ISO older → True", test_strictly_older_iso_older),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\ndiscord-read boundary functions: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

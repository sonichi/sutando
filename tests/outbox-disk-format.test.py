#!/usr/bin/env python3
"""Characterization: pin the outbox ON-DISK FORMAT, not just its API behavior.

The Delivery Core refactor must leave the on-disk layout compatible, but a suite
that drives publish/claim/complete through the API passes just as happily against
a renamed directory. Measured by mutating one name at a time and running the six
existing outbox suites: only CLAIMS_DIR was caught, and only incidentally (one
test pre-creates `.claims` in setup, another globs it to locate a file).
LOCKS_DIR, ITEMS_DIR and STRIPES_FENCE were caught by nothing.

Expected values are spelled out as literals. Comparing a constant to itself, or
rebuilding a name by calling the same helper that produces it, is a tautology
that survives every rename - the golden values below are what make a rename fail.

Run: python3 tests/outbox-disk-format.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# CI runs this with no arguments, so the repo root must be derived, not passed.
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import outbox  # noqa: E402

# sha256(id)[:16], computed independently of _safe_key.
GOLDEN = {"item-1": "59908df50572502c", "a/b": "c14cddc033f64b9d", "a_b": "648fa9b31bc7ff7e"}


def test_on_disk_names_are_literal():
    """The four names that define the layout."""
    assert outbox.CLAIMS_DIR == ".claims", outbox.CLAIMS_DIR
    assert outbox.LOCKS_DIR == ".claim-locks", outbox.LOCKS_DIR
    assert outbox.ITEMS_DIR == ".items", outbox.ITEMS_DIR
    assert outbox.STRIPES_FENCE == "stripes-active.json", outbox.STRIPES_FENCE


def test_claim_path_shape():
    """<root>/.claims/<readable>.<sha256[:16]>.claim"""
    with tempfile.TemporaryDirectory() as tmp:
        p = outbox._claim_path(tmp, "item-1")
        assert p == Path(tmp) / ".claims" / "item-1.59908df50572502c.claim", p
        assert p.parent.name == ".claims", p.parent.name
        assert p.suffix == ".claim", p.suffix


def test_safe_key_stays_injective_across_the_lossy_sanitizer():
    """'a/b' and 'a_b' sanitize alike, so the digest is what keeps them apart.

    If this ever collides, two unrelated items share one claim file and one of
    them is silently denied delivery.
    """
    assert outbox._safe_key("a/b") == f"a_b.{GOLDEN['a/b']}", outbox._safe_key("a/b")
    assert outbox._safe_key("a_b") == f"a_b.{GOLDEN['a_b']}", outbox._safe_key("a_b")
    assert outbox._safe_key("a/b") != outbox._safe_key("a_b")


def test_stripe_fence_path_shape():
    """<root>/.claim-locks/stripes-active.json"""
    with tempfile.TemporaryDirectory() as tmp:
        p = outbox._fence_path(tmp)
        assert p == Path(tmp) / ".claim-locks" / "stripes-active.json", p


def test_claim_dir_is_created_where_the_constant_says():
    """Drive the production writer and assert the layout it actually made."""
    with tempfile.TemporaryDirectory() as tmp:
        assert outbox.acquire_delivery_claim(tmp, "item-1", drainer_id="d0")
        on_disk = {e.name for e in os.scandir(tmp) if e.is_dir()}
        assert ".claims" in on_disk, on_disk
        claims = {e.name for e in os.scandir(Path(tmp) / ".claims")}
        assert claims == {"item-1.59908df50572502c.claim"}, claims


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("PASS - outbox on-disk format pinned")
